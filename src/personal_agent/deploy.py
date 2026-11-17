"""Single-service control-plane entry point for a persistent Railway volume.

Only the minimal volume setup runs as root. The application and administrative commands then
run as the fixed unprivileged account from the image. No user's TOML is overwritten.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.config import Config

RUNTIME_UID = 10001
RUNTIME_GID = 10001


def prepare_home(home: Path) -> Path:
    """Prepare only the volume root, never recursively follow/chown user-controlled files."""
    if not home.is_absolute() or home == Path("/") or home != home.resolve():
        raise ValueError("PA_HOME must be an absolute directory without symlinks")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not home.is_dir() or home.is_symlink():
        raise ValueError("PA_HOME must be a directory without symlinks")
    if os.geteuid() == 0:
        os.chown(home, RUNTIME_UID, RUNTIME_GID, follow_symlinks=False)
        home.chmod(0o700)
        os.setgroups([])
        os.setgid(RUNTIME_GID)
        os.setuid(RUNTIME_UID)
    elif home.stat().st_uid != os.geteuid():
        raise PermissionError("PA_HOME must be owned by the service account")
    home.chmod(0o700)
    return home


def validate_control_config(config: Config) -> None:
    """Fail before importing plugins or opening channels if deployment is unsafe."""
    if not config.web.enabled or not (config.web.public_url or "").startswith("https://"):
        raise ValueError("Set PA_WEB_PUBLIC_URL to the service's HTTPS origin")
    if config.tools.shell:
        raise ValueError("The deployed control plane must disable the local shell")
    if config.plugins.enabled:
        raise ValueError("Python plugins must be disabled on the deployed control plane")
    if config.mcp.enabled and any(
        server.enabled and server.transport == "stdio" for server in config.mcp.servers
    ):
        raise ValueError("Use remote HTTP MCP services instead of stdio in the control plane")


def main() -> None:
    os.umask(0o077)
    os.environ["PA_CONTROL_ONLY"] = "1"
    try:
        home = prepare_home(Path(os.environ.get("PA_HOME", "/data")))
    except (OSError, ValueError):
        raise SystemExit(
            "Cannot prepare PA_HOME. Mount the existing volume at /data; existing data must "
            "be owned by UID/GID 10001. The initializer only adjusts the volume root."
        ) from None
    os.environ["PA_HOME"] = str(home)

    # Import the application only after privilege dropping. In particular, operator-configured
    # extensions are never loaded by the root initializer.
    from personal_agent.config import ConfigError, load_config, load_env
    from personal_agent.paths import Paths

    try:
        paths = Paths(home)
        load_env(paths)
        config = load_config(paths)
        validate_control_config(config)
    except (ConfigError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    if sys.argv[1:2] == ["admin"]:
        from personal_agent.cli import app as cli

        cli(args=sys.argv[2:], prog_name="pa")
        return
    if len(sys.argv) != 1:
        raise SystemExit("Usage: python -m personal_agent.deploy [admin PA_COMMAND ...]")

    from personal_agent.app import build_app
    from personal_agent.channels.registry import build_channels
    from personal_agent.commands.serve import _run
    from personal_agent.daemon import Daemon
    from personal_agent.logs import setup_logging

    with build_app(home=home, config=config, read_env=False) as agent:
        setup_logging(agent.paths, console=True)
        channels = build_channels(agent)
        daemon = Daemon(
            agent,
            channels.channels,
            services=channels.services,
            grace_s=config.runtime.shutdown_grace_s,
        )
        asyncio.run(_run(daemon))


if __name__ == "__main__":
    main()
