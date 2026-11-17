"""Mutations for iteration 4b: the web page's configuration, doctor checks and plugin channels.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration4_web_config.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

DOC = ["tests/test_doctor_channels.py"]
CFG = ["tests/test_web_config.py", "tests/test_config_template.py", "tests/test_channel_config.py"]
WEBCLI = ["tests/test_cli_web.py"]
PHONE_LINKS = ["tests/test_cli_web.py", "tests/test_phone_connect.py"]
HOOKCLI = ["tests/test_cli_webhooks.py"]
CHANS = ["tests/test_cli_channels.py"]
PLUG = ["tests/test_plugin_channels.py", "tests/test_plugins.py"]

D = "src/personal_agent/doctor.py"
C = "src/personal_agent/config.py"
W = "src/personal_agent/commands/web.py"
H = "src/personal_agent/commands/webhooks.py"
CH = "src/personal_agent/commands/channels.py"
PR = "src/personal_agent/plugins/registry.py"
CR = "src/personal_agent/channels/registry.py"
SV = "src/personal_agent/channels/webhooks/service.py"

M = [
    # ---- doctor
    (
        "doctor: other devices check off",
        D,
        "return host not in LOOPBACK_HOSTS",
        "return False",
        DOC,
    ),
    (
        "doctor: https always",
        D,
        'return url is not None and url.startswith("https://")',
        "return True",
        DOC,
    ),
    (
        "doctor: localhost not loopback",
        D,
        'LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})',
        'LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})',
        DOC,
    ),
    (
        "doctor: signed-out browsers counted",
        D,
        '"SELECT COUNT(*) FROM web_sessions WHERE revoked_at IS NULL AND expires_at > ?"',
        '"SELECT COUNT(*) FROM web_sessions WHERE expires_at > ?"',
        DOC,
    ),
    (
        "doctor: expired browsers counted",
        D,
        "revoked_at IS NULL AND expires_at > ?",
        "revoked_at IS NULL AND expires_at < ?",
        DOC,
    ),
    ("doctor: no unpaired warning", D, "if paired == 0:", "if False:", DOC),
    (
        "doctor: port check ignores a running agent",
        D,
        "if EngineLock.is_taken(paths.run_dir / LOCK_NAME):\n        return None",
        "if False:\n        return None",
        DOC,
    ),
    ("doctor: port check never warns", D, "probe.bind((host, port))", "pass", DOC),
    (
        "doctor: ipv6 family",
        D,
        "socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET",
        "socket.AF_INET",
        DOC,
    ),
    ("doctor: no-route not flagged", D, "if not hooks.routes:", "if False:", DOC),
    (
        "doctor: missing secret is ok",
        D,
        "key, problem = route_secret(route, os.environ)",
        'key, problem = b"x", ""',
        DOC,
    ),
    (
        "doctor: events not named",
        D,
        "if route.events:\n                detail += ",
        "if False:\n                detail += ",
        DOC,
    ),
    (
        "doctor: open webhook listener not flagged",
        D,
        "    if _open_to_other_devices(hooks.host) and not _is_https(public):",
        "    if False:",
        DOC,
    ),
    (
        "doctor: http chat public url fine",
        D,
        "    elif public and not _is_https(public) and _public_host(public) not in LOOPBACK_HOSTS:\n        checks = [",
        "    elif False:\n        checks = [",
        DOC,
    ),
    (
        "doctor: http webhook public url fine",
        D,
        "    elif public and not _is_https(public) and _public_host(public) not in LOOPBACK_HOSTS:\n        checks.append(",
        "    elif False:\n        checks.append(",
        DOC,
    ),
    ("doctor: notify web unknown", D, '| ({"web"} if cfg.web.enabled else set())', "| set()", DOC),
    (
        "doctor: plugin notify wording",
        D,
        'if cfg.plugins.enabled and notify not in {"telegram", "web", "dingtalk"}:',
        "if False:",
        DOC,
    ),
    (
        "doctor: webhook failures are warnings",
        D,
        'checks.append(Check(label, "fail", f"{problem}; deliveries are refused", fix))',
        'checks.append(Check(label, "warn", f"{problem}; deliveries are refused", fix))',
        DOC,
    ),
    # ---- service
    (
        "service: empty secret message",
        SV,
        '    if not secret.strip():\n        return None, f"{config.secret_env} is not set (put it in .env)"',
        '    if False:\n        return None, ""',
        DOC + ["tests/test_webhook_service.py"],
    ),
    # ---- config
    (
        "config: env names may be lowercase",
        C,
        '_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")',
        '_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")',
        CFG,
    ),
    ("config: env names unbounded", C, "{0,63}", "{0,6300}", CFG),
    (
        "config: broker env unchecked",
        C,
        'return _check_env_name(value, "env")',
        "return value",
        CFG,
    ),
    (
        "config: telegram token_env unchecked",
        C,
        'return _check_env_name(value, "token_env")',
        "return value",
        CFG,
    ),
    (
        "config: webhook secret_env unchecked",
        C,
        'return _check_env_name(value, "secret_env")',
        "return value",
        CFG,
    ),
    (
        "config: errors echo everything",
        C,
        'raise ConfigError(f"invalid {paths.config_file}:\\n{_describe(exc)}") from None',
        'raise ConfigError(f"invalid {paths.config_file}:\\n{exc}") from None',
        CFG,
    ),
    (
        "config: value error prefix kept",
        "src/personal_agent/validation.py",
        '.removeprefix("Value error, ")',
        "",
        CFG,
    ),
    (
        "config: any host name accepted",
        C,
        "    if _HOSTNAME.fullmatch(host):\n        return host",
        "    if True:\n        return host",
        CFG,
    ),
    (
        "config: brackets loose",
        C,
        'bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host',
        'bare = host.strip("[]")',
        CFG,
    ),
    (
        "config: ipv6 not normalised",
        C,
        "return str(ipaddress.ip_address(bare))",
        "return host",
        CFG,
    ),
    (
        "config: public url keeps case",
        C,
        'return f"{parts.scheme}://{parts.netloc}".lower()',
        'return f"{parts.scheme}://{parts.netloc}"',
        CFG,
    ),
    (
        "config: allowed hosts keep case",
        C,
        "return [name.lower() for name in value]",
        "return value",
        CFG,
    ),
    # ---- pa web
    # The pairing link is built twice: by `pa web pair` (and connect) and by `pa web open`.
    (
        "web: link in the query",
        W,
        'url = f"{agent.config.web.address}/#pair={code}"\n    minutes =',
        'url = f"{agent.config.web.address}/?pair={code}"\n    minutes =',
        WEBCLI,
    ),
    (
        "web: open's link in the query",
        W,
        'url = f"{agent.config.web.address}/#pair={invite.code}"\n        if webbrowser.open(url):',
        'url = f"{agent.config.web.address}/?pair={invite.code}"\n        if webbrowser.open(url):',
        WEBCLI,
    ),
    (
        "web: pair works when off",
        W,
        "        _require_enabled(agent)\n        _pair_link(agent, label, qr, principal=principal)",
        "        _pair_link(agent, label, qr, principal=principal)",
        WEBCLI,
    ),
    (
        "web: unpair cancels nothing",
        W,
        "cancelled = agent.web_auth.cancel_invites()",
        "cancelled = 0",
        WEBCLI,
    ),
    (
        "web: revoke all does nothing",
        W,
        "{agent.web_auth.revoke_all()}",
        "{0}",
        WEBCLI,
    ),
    (
        "web: revoke argument check",
        W,
        "if (session_id is None) == (not everything):",
        "if False:",
        WEBCLI,
    ),
    (
        "web: qr for local addresses",
        W,
        "    if _reachable_from_a_phone(url):\n        if qr:",
        "    if True:\n        if qr:",
        WEBCLI,
    ),
    (
        "web: qr flag ignored",
        W,
        "        if qr:\n            echo(qr_text(url))",
        "        if True:\n            echo(qr_text(url))",
        WEBCLI,
    ),
    (
        "web: everything reachable",
        W,
        '    if host.lower() == "localhost":\n        return False\n    try:\n        return not ipaddress.ip_address(host).is_loopback\n    except ValueError:\n        return bool(host)',
        "    return True",
        PHONE_LINKS,
    ),
    (
        "web: localhost reachable",
        W,
        '    if host.lower() == "localhost":\n        return False',
        "    if False:\n        return False",
        PHONE_LINKS,
    ),
    (
        "web: loopback address reachable",
        W,
        "        return not ipaddress.ip_address(host).is_loopback",
        "        return True",
        PHONE_LINKS,
    ),
    (
        "web: a host name is not reachable",
        W,
        "    except ValueError:\n        return bool(host)",
        "    except ValueError:\n        return False",
        PHONE_LINKS,
    ),
    ("web: label not escaped", W, 'escape(session.label or "-"),', 'session.label or "-",', WEBCLI),
    (
        "web: link not soft-wrapped",
        "src/personal_agent/commands/common.py",
        "console.print(text, highlight=False, soft_wrap=True)",
        "console.print(text, highlight=False)",
        [*WEBCLI, "tests/test_cli_rendering.py"],
    ),
    (
        "cli: echo wraps a long word",
        "src/personal_agent/commands/common.py",
        "console.print(text, markup=False, highlight=False, soft_wrap=True)",
        "console.print(text, markup=False, highlight=False)",
        ["tests/test_cli_rendering.py"],
    ),
    (
        "web: always serving",
        W,
        'serving = "serving" if running else "[yellow]not serving: run `pa serve`[/yellow]"',
        'serving = "serving"',
        WEBCLI,
    ),
    (
        "web: no serve hint after pairing",
        W,
        '    if daemon_state(agent).kind != "daemon":\n        say("[yellow]Nothing is serving',
        '    if False:\n        say("[yellow]Nothing is serving',
        WEBCLI,
    ),
    # ---- pa webhooks
    ("hooks: signs with a fixed time", H, "now=time.time()", "now=0.0", HOOKCLI),
    ("hooks: no confirmation", H, "if wakes and not yes:", "if False:", HOOKCLI),
    (
        "hooks: ping wakes the agent",
        H,
        'wakes = not (route.scheme == "github" and (event or "ping") == "ping")',
        "wakes = True",
        HOOKCLI,
    ),
    ("hooks: no 404 hint", H, "if reply.status_code == 404:", "if False:", HOOKCLI),
    (
        "hooks: failures exit 0",
        H,
        "        if reply.status_code >= 400:\n            raise typer.Exit(1)",
        "        if False:\n            raise typer.Exit(1)",
        HOOKCLI,
    ),
    (
        "hooks: unusable route is tried",
        H,
        '        if not usable.usable:\n            fail("This route cannot be used:", usable.problem)',
        '        if False:\n            fail("This route cannot be used:", usable.problem)',
        HOOKCLI,
    ),
    (
        "hooks: secret shape",
        H,
        "{escape(new_secret(scheme))}",
        '{escape(new_secret("github"))}',
        HOOKCLI,
    ),
    (
        "hooks: secret line without the name",
        H,
        'say(f"  {escape(env_name)}={escape(new_secret(scheme))}")',
        'say(f"  WEBHOOK_SECRET={escape(new_secret(scheme))}")',
        HOOKCLI,
    ),
    ("hooks: refusals hidden", H, 'if refusal != "none":', "if False:", HOOKCLI),
    (
        "hooks: always ready",
        H,
        'state = "[green]ready[/green]" if route.usable else "[red]not ready[/red]"',
        'state = "[green]ready[/green]"',
        HOOKCLI,
    ),
    (
        "hooks: problem hidden",
        H,
        "            if not route.usable:\n                say(",
        "            if False:\n                say(",
        HOOKCLI,
    ),
    ("hooks: tunnel hint always", H, "if not cfg.public_url:", "if True:", HOOKCLI),
    ("hooks: count of all time", H, "now - 86400", "0", HOOKCLI),
    ("hooks: body from a file", H, 'if text.startswith("@"):', "if False:", HOOKCLI),
    (
        "hooks: wildcard host",
        H,
        'if host in ("0.0.0.0", "", "::"):  # noqa: S104 - comparing, not binding\n        return "127.0.0.1"',
        'if False:\n        return "127.0.0.1"',
        HOOKCLI,
    ),
    # ---- pa channels
    ("channels: browsers not counted", CH, "str(len(agent.web_auth.sessions())),", '"0",', CHANS),
    (
        "channels: links not counted",
        CH,
        "str(len(agent.web_auth.pending_invites())),",
        '"0",',
        CHANS,
    ),
    (
        "channels: plugin rows dropped",
        CH,
        "for name, spec in sorted(agent.plugin_channels.items()):",
        "for name, spec in []:",
        CHANS,
    ),
    (
        "channels: web off text",
        CH,
        'else "[dim]off[/dim] (\\\\[web] enabled = true)",',
        'else "[green]on[/green]",',
        CHANS,
    ),
    # ---- plugin channels
    (
        "plugins: reserved names ignored",
        PR,
        "        if name in RESERVED_CHANNELS:",
        "        if False:",
        PLUG,
    ),
    (
        "plugins: duplicate channel accepted",
        PR,
        "        if name in self._channels:",
        "        if False:",
        PLUG,
    ),
    (
        "plugins: name pattern loosened",
        PR,
        'CHANNEL_NAME = re.compile(r"[a-z][a-z0-9-]{1,23}")',
        'CHANNEL_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{1,23}")',
        PLUG,
    ),
    (
        "plugins: rollback keeps channels",
        PR,
        "            self._channels.pop(channel, None)",
        "            pass",
        PLUG,
    ),
    (
        "plugins: non-callable factory",
        PR,
        "        if not callable(factory):",
        "        if False:",
        PLUG,
    ),
    (
        "plugins: push flag as given",
        PR,
        "ChannelSpec(name, factory, bool(push), self.name)",
        "ChannelSpec(name, factory, push, self.name)",
        PLUG,
    ),
    (
        "registry: factory failure escapes",
        CR,
        "        except Exception as exc:  # a plugin's code: its failure costs its channel only",
        "        except ZeroDivisionError as exc:",
        PLUG,
    ),
    (
        "registry: name mismatch accepted",
        CR,
        'if getattr(channel, "name", None) != name:',
        "if False:",
        PLUG,
    ),
    ("registry: long problems kept whole", CR, "[:300])", "[:30000])", PLUG),
    (
        "registry: listening channels not supervised",
        CR,
        'if callable(getattr(channel, "run", None)):',
        "if False:",
        PLUG,
    ),
]
