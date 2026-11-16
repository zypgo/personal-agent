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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from personal_agent.clock import get_tz, valid_zone
from personal_agent.config import Config, ConfigError, api_key, load_config, read_text_file
from personal_agent.db import SCHEMA_VERSION
from personal_agent.harness.pricing import is_known
from personal_agent.maintenance import footprint, size
from personal_agent.paths import Paths
from personal_agent.presence import LOCK_NAME, EngineLock

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.launchd import ServiceManager

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


def _env_file_check(paths: Paths) -> Check:
    if not paths.env_file.exists():
        return Check("env file", "warn", "no .env file", "Run: pa init")
    try:
        read_text_file(paths.env_file)
    except ConfigError as exc:
        return Check("env file", "fail", str(exc), f"Save {paths.env_file} as UTF-8 text")
    if problems := paths.env_file_warnings():
        return Check("env file", "warn", problems[0])
    return Check("env file", "ok", "private (owner only)")


def _home_check(paths: Paths) -> Check:
    if not paths.home.is_dir():
        return Check("home", "fail", f"{paths.home} does not exist", "Run: pa init")
    mode = stat.S_IMODE(paths.home.stat().st_mode)
    if mode & 0o077:
        return Check(
            "home",
            "warn",
            f"{paths.home} is accessible by other users ({mode:04o})",
            f"chmod 700 {paths.home}",
        )
    return Check("home", "ok", str(paths.home))


def _key_check(paths: Paths, cfg: Config) -> Check:
    if api_key():
        return Check("OpenAI key", "ok", "OPENAI_API_KEY is set")
    if cfg.model.base_url or os.environ.get("OPENAI_BASE_URL"):
        return Check("OpenAI key", "warn", "no key, but a custom endpoint is configured")
    return Check(
        "OpenAI key",
        "fail",
        "OPENAI_API_KEY is not set",
        f"Put OPENAI_API_KEY=... in {paths.env_file}",
    )


def _model_checks(cfg: Config) -> list[Check]:
    checks = []
    for role, name in (("main model", cfg.model.main), ("fast model", cfg.model.fast)):
        if is_known(name):
            checks.append(Check(role, "ok", name))
        else:
            detail = f"{name}: no price on file, so cost estimates assume the highest rate"
            checks.append(Check(role, "warn", detail))
    return checks


def _timezone_check(cfg: Config) -> Check:
    zone = cfg.agent.timezone
    if not zone:
        return Check("timezone", "ok", f"detected: {get_tz(None)}")
    if valid_zone(zone):
        return Check("timezone", "ok", zone)
    return Check("timezone", "warn", f"unknown timezone {zone!r}; using the system's")


def _sqlite_check() -> Check:
    if _fts5_available():
        return Check("sqlite", "ok", f"{sqlite3.sqlite_version}, FTS5 available")
    return Check(
        "sqlite",
        "warn",
        f"{sqlite3.sqlite_version} without FTS5",
        "Memory search will be slower and less precise.",
    )


EMPTY_SPACE_WARN_BYTES = 50_000_000
"""A database is worth mentioning when this much of it, and half of it, is space deleted rows left."""


def _database_check(paths: Paths) -> Check:
    if not paths.db_file.exists():
        return Check("database", "warn", "not created yet", "Run: pa init")
    try:
        conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True)
        try:
            found = footprint(conn, paths.db_file, rows=False)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("database", "fail", f"cannot open {paths.db_file.name}: {exc}")
    if found.schema > SCHEMA_VERSION:
        return Check(
            "database",
            "fail",
            f"schema v{found.schema} is newer than this program (v{SCHEMA_VERSION})",
            "Upgrade personal-agent.",
        )
    if exposed := [path for path in paths.exposed_files() if path != paths.env_file]:
        names = ", ".join(path.name for path in exposed)
        return Check(
            "database",
            "warn",
            f"{names} can be read by other users",
            f"Run: pa init (it makes them private), or chmod 600 on {names}",
        )
    detail = f"schema v{found.schema}, {size(found.total_bytes)}"
    if (
        found.reclaimable_bytes >= EMPTY_SPACE_WARN_BYTES
        and found.reclaimable_bytes * 2 >= found.db_bytes
    ):
        return Check(
            "database",
            "warn",
            f"{detail}, over half of it is empty space ({size(found.reclaimable_bytes)})",
            "Stop the agent, then run: pa db vacuum",
        )
    return Check("database", "ok", detail)


def _preferences_check(paths: Paths) -> Check:
    unreadable = []
    files = sorted(paths.preferences_dir.glob("*")) if paths.preferences_dir.is_dir() else []
    for path in files:
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{path.name}: {exc}")
    if unreadable:
        return Check(
            "preferences",
            "fail",
            "; ".join(unreadable),
            "The agent refuses to run without being able to read your rules.",
        )
    return Check("preferences", "ok", str(paths.preferences_dir))


def static_checks(paths: Paths) -> list[Check]:
    """Everything that can be known from the home directory alone, in the order a person would
    look: the home, then its settings, then what the settings point at. It stops where there is
    nothing to read on (no home, a config that does not parse)."""
    python = Check("python", "ok", sys.version.split()[0])  # pyproject.toml sets the minimum
    home = _home_check(paths)
    if home.status == "fail":
        return [python, home]
    checks = [python, home, _env_file_check(paths)]

    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        return [*checks, Check("config", "fail", str(exc), f"Fix {paths.config_file}")]
    where = str(paths.config_file) if paths.config_file.exists() else "defaults (no config.toml)"
    checks.append(Check("config", "ok", where))

    return [
        *checks,
        _key_check(paths, cfg),
        *_model_checks(cfg),
        _timezone_check(cfg),
        _sqlite_check(),
        _database_check(paths),
        _preferences_check(paths),
    ]


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


def _sandbox_check(paths: Paths, cfg: Config) -> Check:
    from personal_agent.security.sandbox import SandboxProvider

    if not cfg.tools.shell:
        return Check("shell", "ok", "switched off in config.toml")
    sandbox, reason = SandboxProvider(paths, cfg.sandbox).resolve()
    if sandbox is None:
        return Check(
            "shell sandbox",
            "warn",
            f"the shell tool is off: {reason}",
            "On macOS the built-in sandbox needs /usr/bin/sandbox-exec; elsewhere install "
            f"Docker and run `docker pull {cfg.sandbox.image}`.",
        )
    if sandbox.name == "none":
        return Check(
            "shell sandbox", "warn", sandbox.describe(), "Remove backend = none to isolate it."
        )
    return Check("shell sandbox", "ok", sandbox.describe())


def _secret_checks(paths: Paths, cfg: Config) -> list[Check]:
    checks = []
    for name, secret in cfg.secrets.items():
        if os.environ.get(secret.env):
            hosts = ", ".join(secret.hosts)
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


def capability_checks(paths: Paths) -> list[Check]:
    """What the agent will be able to do on this machine: sandbox, secrets for the web tools."""
    try:
        cfg = load_config(paths)
    except ConfigError:
        return []  # static_checks reports the broken config
    return [
        _sandbox_check(paths, cfg),
        *_web_checks(cfg),
        *_skills_checks(paths, cfg),
        *_mcp_checks(paths, cfg),
        *_plugin_checks(cfg),
        *_channel_checks(paths, cfg),
        *_chat_page_checks(paths, cfg),
        *_webhook_checks(paths, cfg),
        *_service_checks(paths),
        *_secret_checks(paths, cfg),
    ]


def _service_checks(paths: Paths, manager: ServiceManager | None = None) -> list[Check]:
    """The background service, if one was installed for this home: loaded, still pointing at a
    program that exists, and not somewhere macOS keeps background jobs out of."""
    platform = sys.platform
    if platform != "darwin":
        return []
    from personal_agent.launchd import ServiceError, ServiceManager, protected_folder

    manager = manager or ServiceManager(paths)
    if not manager.plist_file.exists():
        return []  # nobody asked for one: nothing to report
    python = manager.installed_python()
    if python is None:
        return [
            Check(
                "background service",
                "warn",
                f"{manager.plist_file} is not a job file this version wrote",
                "Run: pa service install",
            )
        ]
    if not os.path.exists(python):
        return [
            Check(
                "background service",
                "warn",
                f"it runs {python}, which no longer exists",
                "Run: pa service install (from where the agent is installed now)",
            )
        ]
    if folder := protected_folder(python):
        return [
            Check(
                "background service",
                "warn",
                f"it runs {python}, in your {folder} folder; macOS keeps background jobs out of it",
                "Move the project elsewhere (or `uv tool install .`), then: pa service install",
            )
        ]
    try:
        status = manager.status()
    except ServiceError as exc:
        return [Check("background service", "warn", str(exc))]
    if not status.loaded:
        return [
            Check(
                "background service",
                "warn",
                "installed, but not loaded (it loads at your next login)",
                "Run: pa service install",
            )
        ]
    if status.pid is None:
        return [
            Check(
                "background service",
                "warn",
                f"loaded but not running ({status.state or 'unknown'}; last exit {status.last_exit})",
                "See why: pa service logs --launchd",
            )
        ]
    return [Check("background service", "ok", f"running (pid {status.pid})")]


def _skills_checks(paths: Paths, cfg: Config) -> list[Check]:
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


def _mcp_checks(paths: Paths, cfg: Config) -> list[Check]:
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


def _approved_senders(paths: Paths, channel: str) -> int | None:
    """How many accounts are approved on `channel` (None if the database cannot say)."""
    if not paths.db_file.exists():
        return 0  # nothing has run yet, so nobody can have been approved
    try:
        conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM senders WHERE channel = ?", (channel,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else 0


def _channel_checks(paths: Paths, cfg: Config) -> list[Check]:
    """Chat channels: is what they need there, and will unprompted messages have somewhere to go?"""
    checks: list[Check] = []
    telegram = cfg.telegram
    if telegram.enabled:
        if not os.environ.get(telegram.token_env):
            checks.append(
                Check(
                    "telegram",
                    "fail",
                    f"enabled, but {telegram.token_env} is not set",
                    f"Create a bot with @BotFather and put {telegram.token_env}=... in {paths.env_file}",
                )
            )
        else:
            approved = _approved_senders(paths, "telegram")
            if approved == 0:
                checks.append(
                    Check(
                        "telegram",
                        "warn",
                        "the token is set, but no Telegram account is approved yet",
                        "Start `pa serve`, message your bot, then run "
                        "`pa channels approve telegram CODE` with the code it sends back.",
                    )
                )
            else:
                checks.append(
                    Check(
                        "telegram",
                        "ok",
                        "token set"
                        + (f"; {approved} account(s) approved" if approved else "")
                        + (
                            f"; via proxy {telegram.proxy.split('@')[-1]}" if telegram.proxy else ""
                        ),
                    )
                )
        if cfg.media.voice and shutil.which("ffmpeg") is None:
            checks.append(
                Check(
                    "voice notes",
                    "warn",
                    "Telegram voice notes are Ogg/Opus, which the speech API's documented formats "
                    "leave out, and ffmpeg (which converts them) is not installed",
                    "brew install ffmpeg   (or set [media] voice = false)",
                )
            )
    notify = cfg.notify.channel
    known = (
        {"cli", "auto"}
        | ({"telegram"} if telegram.enabled else set())
        | ({"web"} if cfg.web.enabled else set())
    )
    if notify not in known:
        if cfg.plugins.enabled and notify not in {"telegram", "web"}:
            # Doctor does not run plugin code, so it cannot see which channels they offer.
            checks.append(
                Check(
                    "notify channel",
                    "warn",
                    f"[notify] channel = {notify!r} is not a built-in channel; it works only if "
                    "an enabled plugin provides it",
                    "`pa plugins` lists the channels the plugins offer.",
                )
            )
        else:
            checks.append(
                Check(
                    "notify channel",
                    "warn",
                    f"[notify] channel = {notify!r}, but no such channel is switched on",
                    "Messages the agent sends on its own would wait for a channel that never "
                    "runs. Enable it in config.toml or pick another.",
                )
            )
    return checks


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _open_to_other_devices(host: str) -> bool:
    return host not in LOOPBACK_HOSTS


def _is_https(url: str | None) -> bool:
    return url is not None and url.startswith("https://")


def _public_host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def _live_count(paths: Paths, query: str) -> int | None:
    """One number from the database (read-only), or None if it cannot be had (no database yet, an
    older schema)."""
    if not paths.db_file.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(query, (time.time(),)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else 0


def _port_check(name: str, paths: Paths, host: str, port: int) -> Check | None:
    """Can `pa serve` take this address? Asked only while nothing is running the agent: a running
    agent holds its own ports."""
    import ipaddress

    if EngineLock.is_taken(paths.run_dir / LOCK_NAME):
        return None
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET  # "localhost"
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server does
        probe.bind((host, port))
    except OSError as exc:
        return Check(
            f"{name} port",
            "warn",
            f"{host}:{port} cannot be used: {exc.strerror or exc}",
            f"Stop the program that uses it, or choose another port under [{name}] in config.toml.",
        )
    finally:
        probe.close()
    return None


def _chat_page_checks(paths: Paths, cfg: Config) -> list[Check]:
    """The chat page: where it can be reached from, and whether any browser is paired."""
    web = cfg.web
    if not web.enabled:
        return []
    where = f"{web.host}:{web.port}"
    public = web.public_url
    if _open_to_other_devices(web.host) and not _is_https(public):
        checks = [
            Check(
                "chat page",
                "warn",
                f"listening on {where}: other devices on the network can reach it, and without "
                "HTTPS what you type and read can be seen on the way",
                'Keep host = "127.0.0.1" and put HTTPS in front, for example '
                f"`tailscale serve --bg {web.port}`, then set public_url.",
            )
        ]
    elif public and not _is_https(public) and _public_host(public) not in LOOPBACK_HOSTS:
        checks = [
            Check(
                "chat page",
                "warn",
                f"public_url {public} is not https: the browser will not keep a Secure cookie "
                "and the traffic is readable",
                "Use the https address of your proxy (Tailscale Serve gives one).",
            )
        ]
    else:
        reach = f"reached as {public}" if public else "this computer only"
        checks = [Check("chat page", "ok", f"on {where}; {reach}")]
    paired = _live_count(
        paths, "SELECT COUNT(*) FROM web_sessions WHERE revoked_at IS NULL AND expires_at > ?"
    )
    if paired == 0:
        checks.append(
            Check(
                "chat page browsers",
                "warn",
                "no browser is paired yet",
                "Run `pa web pair` and open the link in the browser (the page cannot be used before).",
            )
        )
    elif paired:
        checks.append(Check("chat page browsers", "ok", f"{paired} paired"))
    if port := _port_check("web", paths, web.host, web.port):
        checks.append(port)
    return checks


def _webhook_checks(paths: Paths, cfg: Config) -> list[Check]:
    """Webhooks: is every route's secret there and strong enough, and how is the listener exposed?"""
    from personal_agent.channels.webhooks.service import route_secret

    hooks = cfg.webhooks
    if not hooks.enabled:
        return []
    if not hooks.routes:
        return [
            Check(
                "webhooks",
                "warn",
                "switched on, but there is no route, so nothing listens",
                "Add [webhooks.routes.NAME] to config.toml (scheme, secret_env, prompt).",
            )
        ]
    checks: list[Check] = []
    for name, route in hooks.routes.items():
        key, problem = route_secret(route, os.environ)
        label = f"webhook {name}"
        if key is None:
            fix = f"Run `pa webhooks secret {name}` and put the line it prints in {paths.env_file}."
            checks.append(Check(label, "fail", f"{problem}; deliveries are refused", fix))
        else:
            detail = f"{route.scheme}; secret set"
            if route.events:
                detail += f"; only {', '.join(route.events)}"
            checks.append(Check(label, "ok", detail))
    where = f"{hooks.host}:{hooks.port}"
    public = hooks.public_url
    if _open_to_other_devices(hooks.host) and not _is_https(public):
        checks.append(
            Check(
                "webhook listener",
                "warn",
                f"listening on {where} without HTTPS: deliveries are signed, but their content "
                "travels in the clear",
                'Put a tunnel or proxy with HTTPS in front and keep host = "127.0.0.1".',
            )
        )
    elif public and not _is_https(public) and _public_host(public) not in LOOPBACK_HOSTS:
        checks.append(
            Check(
                "webhook listener",
                "warn",
                f"public_url {public} is not https: the content of deliveries is readable on the way",
                "Use the https address of your tunnel.",
            )
        )
    elif not public:
        checks.append(
            Check(
                "webhook listener",
                "ok",
                f"on {where}; reachable from this computer only (set public_url once a tunnel "
                "exposes it)",
            )
        )
    else:
        checks.append(Check("webhook listener", "ok", f"on {where}; reached as {public}"))
    if port := _port_check("webhooks", paths, hooks.host, hooks.port):
        checks.append(port)
    return checks


def _plugin_checks(cfg: Config) -> list[Check]:
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


def _web_checks(cfg: Config) -> list[Check]:
    import ipaddress

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
        checks.extend(_fake_ip_check(cfg))

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


def _fake_ip_check(cfg: Config) -> list[Check]:
    """A VPN/proxy in fake-IP mode makes every public name resolve to 198.18.x.x, which the network
    gate refuses by default: all web tools would fail. Say so, with the fix and its price."""
    import ipaddress

    from personal_agent.security.ssrf import FAKE_IP_NETWORK as fake_net
    from personal_agent.security.ssrf import NetPolicy

    fake = []
    for text in _resolve_probe(PROBE_HOST):
        try:
            if ipaddress.ip_address(text) in fake_net:
                fake.append(text)
        except ValueError:
            continue
    if not fake:
        return []
    policy = NetPolicy.from_config(cfg.net)
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
        if run is None:
            checks.append(Check(name, "fail", "the run left no record in the database"))
            break
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
    for message in app.outbox.recent(status="pending"):
        if message.session_key == session:
            app.outbox.cancel(message.id)
    return checks
