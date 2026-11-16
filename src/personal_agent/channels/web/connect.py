"""Phone connection setup: a private LAN listener or a verified Tailscale HTTPS proxy.

Only the local CLI calls this module. Nothing received from a browser or from the model may
change listeners, run Tailscale, or issue a pairing link.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from personal_agent.config import WebConfig

_LAN = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_TAILNET_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,249}[a-z0-9])?\.ts\.net")
Runner = Callable[[list[str]], str]


class ConnectionError(Exception):
    """Setup stopped without replacing an unrelated service."""


@dataclass(frozen=True)
class Connection:
    via: str
    host: str
    port: int
    url: str

    def settings(self) -> dict[str, Any]:
        return {"enabled": True, "host": self.host, "port": self.port, "public_url": self.url}


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConnectionError(f"Could not run {Path(args[0]).name}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:2000]
        raise ConnectionError(f"{Path(args[0]).name} failed: {detail}")
    return result.stdout


def _json(text: str) -> dict[str, Any]:
    try:
        result = json.loads(text)
    except ValueError as exc:
        raise ConnectionError(
            "Tailscale returned invalid status; update Tailscale and try again."
        ) from exc
    if not isinstance(result, dict):
        raise ConnectionError("Tailscale returned an unknown status format.")
    return result


def is_lan_address(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return any(address in network for network in _LAN)


def lan_addresses() -> list[str]:
    """Discover local IPv4 addresses without sending a packet to an outside server."""
    found: set[str] = set()
    try:
        found.update(
            str(item[4][0])
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except OSError:
        pass
    try:
        if sys.platform == "darwin":
            for _, interface in socket.if_nameindex():
                if interface.startswith("en"):
                    try:
                        found.add(_run(["/usr/sbin/ipconfig", "getifaddr", interface]).strip())
                    except ConnectionError:
                        pass
        elif shutil.which("ip"):
            interfaces = json.loads(_run(["ip", "-j", "address", "show", "up"]))
            for interface in interfaces:
                found.update(str(a.get("local", "")) for a in interface.get("addr_info", []))
    except (ConnectionError, OSError, ValueError, TypeError, AttributeError):
        pass
    return sorted(value for value in found if is_lan_address(value))


def lan_connection(host: str | None, port: int) -> Connection:
    if host is None:
        choices = lan_addresses()
        if len(choices) != 1:
            listed = ", ".join(choices) or "none found"
            raise ConnectionError(
                f"Choose this computer's Wi-Fi address with --host. Candidates: {listed}."
            )
        host = choices[0]
    if not is_lan_address(host):
        raise ConnectionError(
            "LAN mode needs a private IPv4 address (10.x, 172.16-31.x, or 192.168.x)."
        )
    local_address(host)
    return Connection("lan", host, port, f"http://{host}:{port}")


def local_address(host: str) -> None:
    """A pairing bearer link must never be issued for somebody else's LAN address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, 0))
    except OSError as exc:
        raise ConnectionError(
            f"{host} is not an available address on this computer. Choose its Wi-Fi address."
        ) from exc


def port_available(port: int, *, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return False
        raise ConnectionError(f"Cannot use the local web port {port}: {exc}") from exc
    return True


def tailscale_binary() -> str:
    command = shutil.which("tailscale")
    if command:
        return command
    mac_app = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if mac_app.is_file():
        return str(mac_app)
    raise ConnectionError(
        "Install Tailscale on this computer and your phone, then sign in on both."
    )


def _serve_configs(config: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield config
    foreground = config.get("Foreground", {})
    if not isinstance(foreground, dict):
        raise ConnectionError("Cannot verify Tailscale's foreground services.")
    for value in foreground.values():
        if not isinstance(value, dict):
            raise ConnectionError("Cannot verify Tailscale's foreground services.")
        yield value


def verify_serve(config: dict[str, Any], name: str, port: int, target: str) -> bool:
    """Return whether our proxy exists. Refuse Funnel and other services on the same origin."""
    endpoint = f"{name}:{port}"
    matched = False
    for item in _serve_configs(config):
        funnel = item.get("AllowFunnel") or {}
        tcp = item.get("TCP") or {}
        web = item.get("Web") or {}
        if not all(isinstance(value, dict) for value in (funnel, tcp, web)):
            raise ConnectionError("Cannot verify this Tailscale Serve configuration.")
        if funnel.get(endpoint):
            raise ConnectionError(
                "Funnel is enabled on this address. Disable it or choose another --https-port."
            )
        listener = tcp.get(str(port))
        site = web.get(endpoint)
        if listener is None and site is None:
            continue
        if (
            not isinstance(listener, dict)
            or listener.get("HTTPS") is not True
            or not isinstance(site, dict)
        ):
            raise ConnectionError("Tailscale already uses this port. Choose another --https-port.")
        handlers = site.get("Handlers")
        if not isinstance(handlers, dict) or set(handlers) != {"/"}:
            raise ConnectionError(
                "Another service uses this HTTPS origin. Choose another --https-port."
            )
        root = handlers["/"]
        proxy = root.get("Proxy") if isinstance(root, dict) else None
        if not isinstance(proxy, str) or proxy.rstrip("/") != target:
            raise ConnectionError(
                "Another service uses this HTTPS origin. Choose another --https-port."
            )
        matched = True
    return matched


def tailscale_connection(
    port: int,
    https_port: int = 443,
    *,
    run: Runner = _run,
    binary: str | None = None,
    verify_target: Callable[[int], None] | None = None,
) -> Connection:
    if verify_target is not None:
        verify_target(port)
    elif not port_available(port):
        raise ConnectionError(
            "The local web port is occupied. Verify the agent or choose another --port."
        )
    command = binary or tailscale_binary()
    status = _json(run([command, "status", "--json"]))
    if status.get("BackendState") != "Running":
        raise ConnectionError(
            "Tailscale is not connected. Sign in and connect it on this computer first."
        )
    own = status.get("Self") or {}
    name = str(own.get("DNSName", "")).rstrip(".").lower() if isinstance(own, dict) else ""
    if not _TAILNET_NAME.fullmatch(name):
        raise ConnectionError(
            "No Tailscale HTTPS name is available. Enable MagicDNS and HTTPS in your tailnet."
        )
    target = f"http://127.0.0.1:{port}"
    before = _json(run([command, "serve", "status", "--json"]))
    if not verify_serve(before, name, https_port, target):
        run([command, "serve", "--bg", f"--https={https_port}", target])
    after = _json(run([command, "serve", "status", "--json"]))
    if not verify_serve(after, name, https_port, target):
        raise ConnectionError(
            "Tailscale has not enabled the proxy. Enable HTTPS/Serve in its admin console, then run this command again."
        )
    suffix = "" if https_port == 443 else f":{https_port}"
    return Connection("tailscale", "127.0.0.1", port, f"https://{name}{suffix}")


def connection_info(config: WebConfig) -> dict[str, Any]:
    url = urlsplit(config.address)
    host = url.hostname or ""
    secure = url.scheme == "https"
    via = (
        "tailscale"
        if secure and host.endswith(".ts.net")
        else "https"
        if secure
        else "lan"
        if is_lan_address(host)
        else "local"
    )
    return {"via": via, "address": config.address, "secure": secure}
