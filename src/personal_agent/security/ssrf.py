"""Outbound HTTP that cannot be pointed at your own computer or network.

An agent that fetches addresses it was handed (by you, by a web page, by a prompt injection) can
be made to knock on doors it should never reach: `http://localhost:11434`, your router, a cloud
metadata service that hands out credentials. Checking the URL text is not enough, because a
harmless-looking name can resolve to a private address, and can resolve to a different one a
moment after it was checked (DNS rebinding). So the check happens where it cannot be bypassed:

- a network backend underneath httpx resolves the name itself, judges *every* address it got, and
  connects to one of the vetted addresses (the original name is still used for TLS and `Host`);
- every redirect is a fresh request that goes through the same gate;
- anything wrapped around an IPv4 address (`::ffff:127.0.0.1`, NAT64, 6to4) is judged as that
  IPv4 address;
- link-local and cloud-metadata addresses are refused even if you allow your private network.

Designs borrowed from hermes-agent's `url_safety.py` (connect-time validation and pinning, in the
`references/` clone); written from scratch against httpcore's public backend interface.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpcore
import httpx

from personal_agent.config import NetConfig

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

MAX_ADDRESSES = 4
"""Addresses tried per name; every answer is still judged."""
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key"})


class Blocked(Exception):
    """The request was refused by network policy. The message says why, for the model."""


# ---- what is never reachable ------------------------------------------------------------------------
_NEVER_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network", includes 0.0.0.0
        "169.254.0.0/16",  # link-local: cloud metadata lives here
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, includes the broadcast address
        "::/128",
        "fe80::/10",  # IPv6 link-local
        "ff00::/8",  # IPv6 multicast
        "fd00:ec2::/32",  # AWS metadata over IPv6
    )
)
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
"""The benchmarking range. VPN and proxy tools (Clash, Surge, Shadowrocket, ...) answer DNS queries
with addresses from here in "fake-IP" mode and sort out the real destination themselves."""
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.goog", "metadata"})
_WRAPPERS = (
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped (::ffff:a.b.c.d)
    ipaddress.ip_network("::ffff:0:0:0/96"),  # IPv4-translated (::ffff:0:a.b.c.d)
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
)


def _embedded_v4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address stands for, if it is one of the wrapper forms."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if any(ip in net for net in _WRAPPERS):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in ipaddress.ip_network("2002::/16"):  # 6to4: the IPv4 address is bits 16-47
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def _views(ip: IPAddress) -> list[IPAddress]:
    inner = _embedded_v4(ip)
    return [ip, inner] if inner is not None else [ip]


def _never(ip: IPAddress) -> bool:
    return any(ip in net for net in _NEVER_NETWORKS)


def _parse_ip(text: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 scope id (fe80::1%en0)
    except ValueError:
        return None


@dataclass(frozen=True)
class NetPolicy:
    """Which private addresses the owner has allowed (everything else private is refused)."""

    allow_hosts: frozenset[str] = frozenset()
    allow_nets: tuple[Network, ...] = ()

    @classmethod
    def from_config(cls, cfg: NetConfig) -> NetPolicy:
        hosts: set[str] = set()
        nets: list[Network] = []
        for entry in cfg.allow_private:
            text = entry.strip().lower().rstrip(".")
            if "/" in text:
                nets.append(ipaddress.ip_network(text, strict=False))
            elif (ip := _parse_ip(text)) is not None:
                nets.append(ipaddress.ip_network(ip))
            else:
                hosts.add(text)
        return cls(frozenset(hosts), tuple(nets))

    def is_listed(self, host: str) -> bool:
        """Is this host (a name or an IP literal) one the owner put under allow_private?"""
        name = host.lower().rstrip(".").strip("[]")
        if name in self.allow_hosts:
            return True
        ip = _parse_ip(name)
        return ip is not None and any(ip in net for net in self.allow_nets)

    def _allowed(self, host: str, ip: IPAddress) -> bool:
        return host in self.allow_hosts or any(ip in net for net in self.allow_nets)

    def check(self, host: str, ip: IPAddress) -> str | None:
        """Why this address may not be connected to for `host`, or None."""
        for view in _views(ip):
            if _never(view):
                return (
                    f"{host} resolves to {ip}, a link-local, cloud-metadata or otherwise "
                    "unreachable address"
                )
            if not view.is_global and not self._allowed(host, view):
                if view in FAKE_IP_NETWORK:
                    return (
                        f"{host} resolves to {ip}, an address VPN and proxy tools (Clash, Surge, "
                        "Shadowrocket...) hand out in their fake-IP mode. If you use one, list "
                        f"{FAKE_IP_NETWORK} under [net] allow_private: the proxy then decides where "
                        "connections really go, and this agent can no longer check the destination "
                        "itself"
                    )
                return (
                    f"{host} resolves to {ip}, an address on this computer or your own network. "
                    "If you want the agent to reach it, list it under [net] allow_private"
                )
        return None


def resolve_checked(host: str, port: int, policy: NetPolicy) -> list[str]:
    """Resolve `host` and return the addresses it may be reached at; Blocked if any answer is not
    allowed (a name that points partly at a private address is not trusted at all)."""
    name = host.lower().rstrip(".")
    if not name:
        raise Blocked("the URL has no host")
    if name in _METADATA_HOSTS:
        raise Blocked(f"{host} is a cloud metadata service")
    try:
        answers = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Blocked(f"cannot resolve {host}: {exc.strerror or exc}") from exc
    safe: list[str] = []
    for *_, sockaddr in answers:
        raw = str(sockaddr[0])
        ip = _parse_ip(raw)
        if ip is None:
            raise Blocked(f"{host} resolved to something that is not an IP address ({raw!r})")
        if reason := policy.check(name, ip):
            raise Blocked(reason)
        if raw not in safe:
            safe.append(raw)
    if not safe:
        raise Blocked(f"{host} did not resolve to any address")
    # IPv4 first: a broken IPv6 route makes a connection attempt hang for the whole timeout, while
    # an IPv4-only failure is fast. IPv6-only hosts are still reached (they are tried last).
    safe.sort(key=lambda address: ":" in address)
    return safe[:MAX_ADDRESSES]


# ---- the gate: a network backend that only dials vetted addresses -------------------------------------
class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy: NetPolicy) -> None:
        self._policy = policy
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature is httpcore's)
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await asyncio.to_thread(resolve_checked, host, port, self._policy)
        failure: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure or httpcore.ConnectError(f"could not connect to {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature is httpcore's)
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise Blocked("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


@functools.cache
def _ssl_context() -> ssl.SSLContext:
    return httpx.create_ssl_context(verify=True, trust_env=False)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport with its connection pool rebuilt on the guarded backend."""

    def __init__(self, policy: NetPolicy) -> None:
        super().__init__(verify=_ssl_context(), trust_env=False, http2=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_ssl_context(),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=15.0,
            http1=True,
            http2=False,
            network_backend=GuardedBackend(policy),
        )


# ---- URLs ---------------------------------------------------------------------------------------------------
def check_url(raw: str) -> httpx.URL:
    """A normalized http(s) URL, or Blocked. Refuses forms that different parsers read differently
    (backslashes, userinfo, percent-encoded or control characters in the authority)."""
    text = (raw or "").strip()
    if not text:
        raise Blocked("the URL is empty")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise Blocked("the URL contains control characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise Blocked(f"the URL is not valid: {exc}") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise Blocked("only http:// and https:// URLs can be fetched")
    if not parts.netloc or not parts.hostname:
        raise Blocked("the URL has no host (write it as https://host/path)")
    if "\\" in parts.netloc:
        raise Blocked("the URL contains a backslash in its address")
    if "@" in parts.netloc:
        raise Blocked("URLs with a user name or password are not allowed")
    if "%" in parts.hostname or any(c.isspace() for c in parts.hostname):
        raise Blocked("the URL's host is written in an ambiguous way")
    if port is not None and not 0 < port < 65536:
        raise Blocked("the URL's port is out of range")
    try:
        return httpx.URL(text)
    except httpx.InvalidURL as exc:
        raise Blocked(f"the URL is not valid: {exc}") from exc


def host_of(url: httpx.URL) -> str:
    return url.host.lower().rstrip(".")


def origin_of(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port or (443 if url.scheme == "https" else 80)
    return (url.scheme, host_of(url), port)


# ---- fetching -----------------------------------------------------------------------------------------------
@dataclass
class Fetched:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def charset(self) -> str | None:
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                return value.strip("\"' ").lower()
        return None


Hop = Callable[[httpx.URL, httpx.URL | None], None]
"""Called before every request of a fetch with (url, previous url). Raise Blocked to stop."""


async def fetch(
    cfg: NetConfig,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    max_bytes: int | None = None,
    on_hop: Hop | None = None,
    policy: NetPolicy | None = None,
) -> Fetched:
    """One request plus its redirects, all through the gate. Raises Blocked, or httpx errors for
    network failures. The body is read only up to `max_bytes`."""
    policy = policy or NetPolicy.from_config(cfg)
    limit = max_bytes or cfg.max_response_bytes
    current = check_url(url)
    previous: httpx.URL | None = None
    visited: list[str] = []
    verb = method.upper()
    timeout = httpx.Timeout(cfg.timeout_s, connect=min(10.0, cfg.timeout_s))
    base_headers = {
        "user-agent": cfg.user_agent,
        "accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    }
    async with httpx.AsyncClient(
        transport=GuardedTransport(policy), timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(cfg.max_redirects + 1):
            if on_hop is not None:
                on_hop(current, previous)
            request_headers = {**base_headers, **(headers or {})}
            async with client.stream(
                verb, current, headers=request_headers, content=content
            ) as response:
                if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                    target = check_url(str(current.join(response.headers["location"])))
                    visited.append(str(target))
                    if origin_of(target) != origin_of(current):
                        if content is not None and response.status_code in (307, 308):
                            raise Blocked(
                                "the site redirected a request that carries data to another "
                                "address, which was not followed"
                            )
                        # Credentials belong to the site they were meant for, not to wherever
                        # it sends us.
                        headers = {
                            k: v
                            for k, v in (headers or {}).items()
                            if k.lower() not in CREDENTIAL_HEADERS
                        }
                    previous, current = current, target
                    if response.status_code in (301, 302, 303) and verb not in ("GET", "HEAD"):
                        verb, content = "GET", None  # what browsers do; the body is not re-sent
                        headers = {
                            k: v
                            for k, v in (headers or {}).items()
                            if k.lower() not in ("content-type", "content-length")
                        }
                    continue
                body = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    room = limit - len(body)
                    body += chunk[:room]
                    if len(chunk) > room:
                        truncated = True
                        break
                return Fetched(
                    url=str(current),
                    status=response.status_code,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=bytes(body),
                    truncated=truncated,
                    redirects=visited,
                )
    raise Blocked(f"more than {cfg.max_redirects} redirects")
