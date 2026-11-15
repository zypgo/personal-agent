"""`pa doctor`: find setup problems before they surface as confusing runtime failures.

`static_checks` never calls the network and never creates anything. `capability_checks` starts a
trivial process to find out whether the sandbox really isolates (it creates the sandbox's scratch
directories). `live_checks` makes two
real model calls (a few cents) to prove the parts that cannot be unit-tested: the API key, the
tool schemas, and replaying stored history (including encrypted reasoning) back to the API.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import stat
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_agent.clock import get_tz
from personal_agent.config import ConfigError, api_key, load_config
from personal_agent.db import SCHEMA_VERSION
from personal_agent.harness.pricing import is_known
from personal_agent.paths import Paths

if TYPE_CHECKING:
    from personal_agent.app import App

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False
    return True


def static_checks(paths: Paths) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, status: Status, detail: str = "", fix: str = "") -> None:
        checks.append(Check(name, status, detail, fix))

    add("python", "ok", sys.version.split()[0])  # pyproject.toml enforces the minimum version

    if not paths.home.is_dir():
        add("home", "fail", f"{paths.home} does not exist", "Run: pa init")
        return checks
    mode = stat.S_IMODE(paths.home.stat().st_mode)
    if mode & 0o077:
        add(
            "home",
            "warn",
            f"{paths.home} is accessible by other users ({mode:04o})",
            f"chmod 700 {paths.home}",
        )
    else:
        add("home", "ok", str(paths.home))

    if not paths.env_file.exists():
        add("env file", "warn", "no .env file", "Run: pa init")
    elif problems := paths.env_file_warnings():
        add("env file", "warn", problems[0])
    else:
        add("env file", "ok", "private (owner only)")

    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        add("config", "fail", str(exc), f"Fix {paths.config_file}")
        return checks
    add(
        "config",
        "ok",
        str(paths.config_file) if paths.config_file.exists() else "defaults (no config.toml)",
    )

    if api_key():
        add("OpenAI key", "ok", "OPENAI_API_KEY is set")
    elif cfg.model.base_url or os.environ.get("OPENAI_BASE_URL"):
        add("OpenAI key", "warn", "no key, but a custom endpoint is configured")
    else:
        add(
            "OpenAI key",
            "fail",
            "OPENAI_API_KEY is not set",
            f"Put OPENAI_API_KEY=... in {paths.env_file}",
        )

    for role, name in (("main model", cfg.model.main), ("fast model", cfg.model.fast)):
        if is_known(name):
            add(role, "ok", name)
        else:
            add(
                role, "warn", f"{name}: no price on file, so cost estimates assume the highest rate"
            )

    if cfg.agent.timezone:
        try:
            ZoneInfo(cfg.agent.timezone)
            add("timezone", "ok", cfg.agent.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            add("timezone", "warn", f"unknown timezone {cfg.agent.timezone!r}; using the system's")
    else:
        add("timezone", "ok", f"detected: {get_tz(None)}")

    if _fts5_available():
        add("sqlite", "ok", f"{sqlite3.sqlite_version}, FTS5 available")
    else:
        add(
            "sqlite",
            "warn",
            f"{sqlite3.sqlite_version} without FTS5",
            "Memory search will be slower and less precise.",
        )

    if paths.db_file.exists():
        try:
            conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            add("database", "fail", f"cannot open {paths.db_file.name}: {exc}")
        else:
            if version > SCHEMA_VERSION:
                add(
                    "database",
                    "fail",
                    f"schema v{version} is newer than this program (v{SCHEMA_VERSION})",
                    "Upgrade personal-agent.",
                )
            else:
                add("database", "ok", f"schema v{version}")
    else:
        add("database", "warn", "not created yet", "Run: pa init")

    unreadable = []
    for path in sorted(paths.preferences_dir.glob("*")) if paths.preferences_dir.is_dir() else []:
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{path.name}: {exc}")
    if unreadable:
        add(
            "preferences",
            "fail",
            "; ".join(unreadable),
            "The agent refuses to run without being able to read your rules.",
        )
    else:
        add("preferences", "ok", str(paths.preferences_dir))
    return checks


PROBE_HOST = "example.com"


def _resolve_probe(name: str, timeout: float = 3.0) -> list[str]:
    """The addresses DNS gives for `name` on this machine; empty if it cannot be resolved in time."""
    found: list[str] = []

    def work() -> None:
        try:
            found.extend(
                sorted(
                    {str(a[4][0]) for a in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)}
                )
            )
        except OSError:
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    return list(found)


def capability_checks(paths: Paths) -> list[Check]:
    """What the agent will be able to do on this machine: sandbox, secrets for the web tools."""
    from personal_agent.security.sandbox import SandboxProvider

    try:
        cfg = load_config(paths)
    except ConfigError:
        return []  # static_checks reports the broken config
    checks: list[Check] = []

    if not cfg.tools.shell:
        checks.append(Check("shell", "ok", "switched off in config.toml"))
    else:
        sandbox, reason = SandboxProvider(paths, cfg.sandbox).resolve()
        if sandbox is None:
            checks.append(
                Check(
                    "shell sandbox",
                    "warn",
                    f"the shell tool is off: {reason}",
                    "On macOS the built-in sandbox needs /usr/bin/sandbox-exec; elsewhere install "
                    f"Docker and run `docker pull {cfg.sandbox.image}`.",
                )
            )
        elif sandbox.name == "none":
            checks.append(
                Check(
                    "shell sandbox",
                    "warn",
                    sandbox.describe(),
                    "Remove backend = none to isolate it.",
                )
            )
        else:
            checks.append(Check("shell sandbox", "ok", sandbox.describe()))

    checks.extend(_web_checks(cfg))
    checks.extend(_skills_checks(paths, cfg))
    checks.extend(_mcp_checks(paths, cfg))
    checks.extend(_plugin_checks(cfg))

    for name, secret in cfg.secrets.items():
        hosts = ", ".join(secret.hosts)
        if os.environ.get(secret.env):
            checks.append(
                Check(f"secret {name}", "ok", f"{secret.env} is set; sent only to {hosts}")
            )
        else:
            checks.append(
                Check(
                    f"secret {name}",
                    "warn",
                    f"{secret.env} is not set, so {{{{secret:{name}}}}} will not work",
                    f"Put {secret.env}=... in {paths.env_file}",
                )
            )
    return checks


def _skills_checks(paths: Paths, cfg) -> list[Check]:
    from personal_agent.skills.store import SkillStore

    if not cfg.skills.enabled:
        return [Check("skills", "ok", "switched off in config.toml")]
    scan = SkillStore(paths, cfg.skills).scan()
    skipped = [p for p in scan.problems if p.fatal]
    warned = [p for p in scan.problems if not p.fatal]
    if not scan.skills and not skipped:
        return [
            Check(
                "skills",
                "ok",
                "none installed",
                "`pa init` adds the starter skills; `pa skills install <folder or https address>` adds more.",
            )
        ]
    detail = f"{len(scan.skills)} installed"
    if warned:
        detail += f", {len(warned)} warning(s)"
    if skipped:
        shown = "; ".join(f"{p.folder}: {p.message}" for p in skipped[:3])
        return [
            Check("skills", "warn", f"{detail}, {len(skipped)} skipped ({shown})", "Run: pa skills")
        ]
    return [Check("skills", "ok", detail)]


def _reviewed_tool_counts(paths: Paths) -> dict[str, int]:
    """How many MCP tools the owner has reviewed, per server (read-only; empty if unknowable)."""
    if not paths.db_file.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT server, COUNT(*) FROM mcp_pins GROUP BY server").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {str(server): int(count) for server, count in rows}


def _mcp_checks(paths: Paths, cfg) -> list[Check]:
    """MCP servers are programs or services the owner chose: say what would stop them working."""
    if not cfg.mcp.servers:
        return []  # nothing configured, nothing to report
    if not cfg.mcp.enabled:
        return [Check("mcp", "ok", "switched off in config.toml")]
    reviewed = _reviewed_tool_counts(paths)
    checks: list[Check] = []
    for server in cfg.mcp.servers:
        label = f"mcp {server.name}"
        if not server.enabled:
            checks.append(Check(label, "ok", "switched off"))
            continue
        problems: list[str] = []
        hints: list[str] = []
        if server.transport == "stdio" and not shutil.which(
            os.path.expanduser(server.command or "")
        ):
            problems.append(f"the program {server.command!r} was not found")
            hints.append("Install it, or fix `command` in config.toml.")
        missing = [name for name in server.referenced_env() if not os.environ.get(name)]
        if missing:
            problems.append(f"{', '.join(missing)} is not set")
            hints.append(f"Put {missing[0]}=... in {paths.env_file}")
        count = reviewed.get(server.name, 0)
        if count == 0 and server.pin == "ask":
            problems.append("its tools have not been reviewed, so none are offered")
            hints.append(f"Run: pa mcp trust {server.name}")
        if problems:
            checks.append(Check(label, "warn", "; ".join(problems), " ".join(hints)))
        else:
            reviewed_text = f"{count} tool(s) reviewed" if count else "tools accepted on first use"
            checks.append(Check(label, "ok", f"{server.transport}; {reviewed_text}"))
    return checks


def _plugin_checks(cfg) -> list[Check]:
    """Plugins are code the owner switched on: say if one that is enabled is not even installed.
    (Loading them is `pa plugins`' job; doctor does not run plugin code.)"""
    if not cfg.plugins.enabled:
        return []
    from personal_agent.plugins.registry import installed

    present = installed()
    checks = []
    for name in dict.fromkeys(cfg.plugins.enabled):
        if name in present:
            checks.append(Check(f"plugin {name}", "ok", f"installed ({present[name].value})"))
        else:
            checks.append(
                Check(
                    f"plugin {name}",
                    "warn",
                    "switched on in config.toml, but no installed package provides it",
                    f"Install the package, or remove {name!r} from [plugins] enabled.",
                )
            )
    return checks


def _web_checks(cfg) -> list[Check]:
    import ipaddress

    from personal_agent.security.ssrf import FAKE_IP_NETWORK, NetPolicy

    tools = [
        name
        for name, on in (
            ("web_fetch", cfg.tools.web_fetch),
            ("http_request", cfg.tools.http_request),
        )
        if on
    ]
    if cfg.tools.web_search == "off":
        search = "off"
    elif cfg.model.api != "responses":
        search = "unavailable (needs model.api = responses)"
    else:
        search = cfg.tools.web_search
    checks = [Check("web", "ok", f"tools: {', '.join(tools) or 'none'}; hosted search: {search}")]

    if tools:
        checks.extend(_fake_ip_check(cfg, NetPolicy, FAKE_IP_NETWORK))

    broad: list[str] = []
    for entry in cfg.net.allow_private:
        try:
            network = ipaddress.ip_network(entry.strip(), strict=False)
        except ValueError:
            continue  # a host name
        if network.num_addresses > 65_536:
            broad.append(entry)
    if broad:
        checks.append(
            Check(
                "private network access",
                "warn",
                f"net.allow_private opens very large ranges ({', '.join(broad)})",
                "List only the hosts or small networks the agent needs, such as 192.168.1.0/24.",
            )
        )
    elif cfg.net.allow_private:
        checks.append(
            Check("private network access", "ok", f"allowed: {', '.join(cfg.net.allow_private)}")
        )
    return checks


def _fake_ip_check(cfg, policy_type, fake_net) -> list[Check]:
    """A VPN/proxy in fake-IP mode makes every public name resolve to 198.18.x.x, which the network
    gate refuses by default: all web tools would fail. Say so, with the fix and its price."""
    import ipaddress

    fake = []
    for text in _resolve_probe(PROBE_HOST):
        try:
            if ipaddress.ip_address(text) in fake_net:
                fake.append(text)
        except ValueError:
            continue
    if not fake:
        return []
    policy = policy_type.from_config(cfg.net)
    if all(policy.check(PROBE_HOST, ipaddress.ip_address(a)) is None for a in fake):
        return [
            Check(
                "proxy fake-IP DNS",
                "ok",
                f"a VPN/proxy tool answers DNS with {fake_net} addresses; [net] allow_private allows it",
            )
        ]
    return [
        Check(
            "proxy fake-IP DNS",
            "warn",
            f"{PROBE_HOST} resolves to {fake[0]}: a VPN/proxy tool is answering DNS in fake-IP mode, "
            "so web_fetch and http_request refuse every site",
            f'Add "{fake_net}" to [net] allow_private in config.toml (the proxy then decides where '
            "connections really go and the agent cannot check the destination itself), or turn "
            "fake-IP mode off.",
        )
    ]


LIVE_PROMPTS = (
    "This is a connectivity self-test. Use the memory tool to view /memories, then reply with the "
    "single word READY.",
    "Second self-test turn. In one short sentence, say what I asked you to do in my previous message.",
)


async def live_checks(app: App) -> list[Check]:
    """Two real turns on a throwaway session. Costs a few cents.

    Turn 1 proves the key, the model name and the tool schemas. Turn 2 replays the stored
    history, including reasoning items, back to the API, which is the part a stateless
    (`store = false`) setup can get wrong.
    """
    from personal_agent.core.events import Kind, NewEvent
    from personal_agent.core.worker import Worker

    checks: list[Check] = []
    session = f"doctor-{int(app.clock.now())}"
    worker = Worker(app)
    total = 0.0
    for number, prompt in enumerate(LIVE_PROMPTS, start=1):
        app.inbox.add(
            NewEvent(source="cli", kind=Kind.MESSAGE, session_key=session, payload={"text": prompt})
        )
        result = await worker.run_once(session_key=session, on_failure="drop")
        name = f"live turn {number}"
        if result is None or result.failure is not None or result.outcome is None:
            detail = result.failure.summary if result and result.failure else "no run happened"
            hint = (result.failure.user_hint if result and result.failure else None) or ""
            checks.append(Check(name, "fail", detail, hint))
            break
        run = app.runs.get(result.outcome.run_id)
        total += run.cost_usd
        detail = (
            f"{run.requests} model calls, {run.tool_calls} tool calls, about ${run.cost_usd:.4f}"
        )
        if result.outcome.status != "completed":
            checks.append(Check(name, "fail", f"run ended as {result.outcome.status}: {detail}"))
            break
        if number == 1 and run.tool_calls == 0:
            checks.append(Check(name, "warn", f"the model did not call the memory tool; {detail}"))
        else:
            checks.append(Check(name, "ok", detail))
    else:
        checks.append(
            Check(
                "history replay",
                "ok",
                f"stored history was accepted by the API; total about ${total:.4f}",
            )
        )

    app.sessions.clear(session)
    for message in app.outbox.list(status="pending"):
        if message.session_key == session:
            app.outbox.cancel(message.id)
    return checks
