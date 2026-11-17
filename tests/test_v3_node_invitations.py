"""How many node invitations may wait, and what one may promise."""

from __future__ import annotations

import pytest

from personal_agent.nodes.registry import DEFAULT_CAPABILITIES, NodeRegistry


@pytest.fixture
def registry(db, clock):
    return NodeRegistry(db, clock)


def test_an_invitation_cannot_promise_a_capability_no_node_has(registry):
    with pytest.raises(ValueError, match="capability"):
        registry.create_invite("owner", ["personal-owner"], capabilities=["host_root"])
    with pytest.raises(ValueError, match="capability"):
        registry.create_invite(
            "owner", ["personal-owner"], capabilities=[*DEFAULT_CAPABILITIES, "host_root"]
        )
    assert registry.create_invite("owner", ["personal-owner"], capabilities=["network_none"])


def test_at_most_32_invitations_wait_at_the_same_time(registry, clock):
    codes = [registry.create_invite("owner", ["personal-owner"]).code for _ in range(32)]
    with pytest.raises(ValueError, match="too many"):
        registry.create_invite("owner", ["personal-owner"])

    # A used invitation no longer waits...
    assert registry.redeem(codes[0], DEFAULT_CAPABILITIES)
    assert registry.create_invite("owner", ["personal-owner"])
    with pytest.raises(ValueError, match="too many"):
        registry.create_invite("owner", ["personal-owner"])

    # ...and neither does one that has expired.
    clock.advance(601)
    assert registry.create_invite("owner", ["personal-owner"])
