"""The offline tests stay on this machine, and away from an agent that is running on it."""

from __future__ import annotations

import errno
import socket

import pytest

from network_guard import AGENT_PORTS, guard_connects, reason_to_refuse

OTHER_MACHINE = "it is on another machine"
RUNNING_AGENT = "that port is where a running agent listens"


def test_the_ports_an_agent_listens_on_by_default_are_the_ones_kept_away_from():
    assert {8765, 8766} == AGENT_PORTS  # the chat page and the webhook listener


@pytest.mark.parametrize(
    "address",
    [("93.184.216.34", 443), ("192.0.2.1", 80), ("192.168.1.10", 8766), ("::2", 80, 0, 0)],
)
def test_another_machine_is_refused(address):
    assert reason_to_refuse(address) == OTHER_MACHINE


@pytest.mark.parametrize("address", [("example.com", 80), ("mac.local", 8765), ("", 80)])
def test_a_name_nobody_resolved_is_refused_because_it_could_be_anywhere(address):
    assert reason_to_refuse(address) == OTHER_MACHINE


@pytest.mark.parametrize("address", [("fe80::1%lo0", 1), ("fe80::1", 1, 0, 1)])
def test_a_link_local_address_is_another_machine_not_this_one(address):
    assert reason_to_refuse(address) == OTHER_MACHINE


@pytest.mark.parametrize(
    "address",
    [("127.0.0.1", 8765), ("localhost", 8766), ("::1", 8766, 0, 0), ("127.0.0.2", 8765)],
)
def test_a_running_agent_on_this_machine_is_refused(address):
    assert reason_to_refuse(address) == RUNNING_AGENT


@pytest.mark.parametrize(
    "address", [("127.0.0.1", 40123), ("localhost", 9), ("::1", 55555, 0, 0), ("127.0.0.2", 1)]
)
def test_a_server_a_test_started_for_itself_is_not_refused(address):
    assert reason_to_refuse(address) is None


def test_a_unix_socket_is_a_file_on_this_machine():
    assert reason_to_refuse("/tmp/something.sock") is None


def test_a_refused_connect_looks_to_the_code_like_nothing_was_listening(monkeypatch):
    tried = guard_connects(monkeypatch)
    with socket.socket() as sock:
        with pytest.raises(ConnectionRefusedError, match="may not connect") as caught:
            sock.connect(("127.0.0.1", 8766))
        assert caught.value.errno == errno.ECONNREFUSED
        assert sock.connect_ex(("192.0.2.1", 80)) == errno.ECONNREFUSED
    assert tried == [
        f"('127.0.0.1', 8766) ({RUNNING_AGENT})",
        f"('192.0.2.1', 80) ({OTHER_MACHINE})",
    ]


def test_a_connect_to_a_server_the_test_started_goes_through(monkeypatch):
    tried = guard_connects(monkeypatch)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.socket() as client:
            client.connect(server.getsockname())
            accepted, _ = server.accept()
            accepted.close()
    assert tried == []
