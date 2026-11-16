"""What an offline test may connect to: servers it started itself, and nothing else.

Not another machine, and not an agent somebody has running on this one (its chat page and webhook
listener are on the default ports). `guard_connects` is used for every test by `conftest.py`.
"""

from __future__ import annotations

import errno
import ipaddress
import socket

import pytest

from personal_agent.config import Config

AGENT_PORTS = frozenset({Config().web.port, Config().webhooks.port})
"""Where somebody's own agent listens unless they chose other ports."""


def reason_to_refuse(address: object) -> str | None:
    """Why an offline test may not connect to `address` (what `socket.connect` is given), if so."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None  # a unix socket: a file on this machine
    host, port = str(address[0]), address[1]
    if host == "localhost":
        loopback = True
    else:
        try:
            loopback = ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            loopback = False  # a name nobody has resolved yet: it could be anywhere
    if not loopback:
        return "it is on another machine"
    if port in AGENT_PORTS:
        return "that port is where a running agent listens"
    return None


def guard_connects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """From now until the test ends, a `connect` to what `reason_to_refuse` names fails the way it
    would with nothing listening there, and is written to the list this returns.

    The code under test sees an ordinary refusal and fails or copes as it would anyway; the list is
    for the test to judge (the autouse fixture in `conftest.py` fails any test that has an entry),
    so a command that catches the error and carries on does not hide it."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    tried: list[str] = []

    def refused(address: object) -> str | None:
        reason = reason_to_refuse(address)
        if reason is None:
            return None
        tried.append(f"{address} ({reason})")
        return tried[-1]

    def connect(self: socket.socket, address: object) -> None:
        if (what := refused(address)) is not None:
            raise ConnectionRefusedError(errno.ECONNREFUSED, f"a test may not connect to {what}")
        return real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if refused(address) is not None:
            return errno.ECONNREFUSED
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return tried
