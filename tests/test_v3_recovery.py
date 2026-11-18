"""Spending a recovery code ends everything that existed before it (docs/COLLABORATION.md).

The code is for the day every device is lost or suspect. If the old sessions survived it, the
person holding a stolen device would stay signed in after the owner had "recovered"; and so would
anything that device prepared while it was still trusted.
"""

from __future__ import annotations

import pytest

from personal_agent.collaboration.models import OWNER_SPACE, InvalidInvite
from web_fake import Web


def post(web, path, data):
    return web.client.post(path, json=data, headers=web.write)


@pytest.fixture
def browsers(make_app):
    root = make_app()
    webs = {name: Web(root) for name in ("phone", "stolen", "laptop", "thief")}
    yield root, webs
    for web in webs.values():
        web.client.close()


def test_recovery_ends_every_earlier_session_and_every_code_a_session_prepared(browsers):
    root, web = browsers
    phone, stolen, laptop, thief = (web[name] for name in ("phone", "stolen", "laptop", "thief"))
    project = root.identities.create_project("owner", "Shared")
    invited = root.identities.create_member_invite("owner", project.id)
    enrolled = post(phone, "/api/v3/enroll", {"code": invited.code, "display_name": "Sam"})
    assert enrolled.status_code == 200, enrolled.text
    recovery = enrolled.json()["recovery_code"]
    sam = phone.client.get("/api/v3/me").json()["principal"]["id"]

    # The second device is the one that gets lost. While it is still trusted, whoever holds it
    # prepares other ways back in: a spare device code, a chat binding, a project invitation.
    tablet = post(phone, "/api/v3/devices/pair", {"label": "tablet"}).json()["code"]
    assert post(stolen, "/api/pair", {"code": tablet}).status_code == 200
    mine = post(stolen, "/api/v3/spaces", {"name": "Mine"}).json()["space"]["id"]
    spare = {
        "device": post(stolen, "/api/v3/devices/pair", {"label": "spare"}).json()["code"],
        "legacy_device": root.web_auth.invite("spare", principal_id=sam).code,
        "channel": post(stolen, "/api/v3/channel-binding", {}).json()["code"],
        "member": post(stolen, f"/api/v3/invites?space={mine}", {}).json()["code"],
    }

    recovered = post(laptop, "/api/v3/recover", {"code": recovery})
    assert recovered.status_code == 200, recovered.text

    # Only the new device is signed in, and it is the same person.
    assert laptop.client.get("/api/v3/me").json()["principal"]["id"] == sam
    for earlier in (phone, stolen):
        assert earlier.client.get("/api/v3/me").status_code == 401
    # Nothing prepared before the recovery can be spent after it.
    assert post(thief, "/api/pair", {"code": spare["device"]}).status_code == 403
    assert post(thief, "/api/pair", {"code": spare["legacy_device"]}).status_code == 403
    with pytest.raises(InvalidInvite):
        root.identities.bind_channel(spare["channel"], "telegram", "bot", "sender", "sender")
    with pytest.raises(InvalidInvite):
        root.identities.redeem_member(spare["member"], label="Friend")
    # And the spent recovery code does not open a second door.
    assert post(thief, "/api/v3/recover", {"code": recovery}).status_code == 400
    assert thief.client.get("/api/v3/me").status_code == 401


def test_recovery_leaves_other_people_and_their_codes_alone(browsers):
    root, web = browsers
    a, b, new_device, third_device = (web[n] for n in ("phone", "laptop", "thief", "stolen"))
    project = root.identities.create_project("owner", "Shared")
    first = root.identities.create_member_invite("owner", project.id)
    second = root.identities.create_member_invite("owner", project.id)
    assert post(a, "/api/v3/enroll", {"code": first.code, "display_name": "A"}).status_code == 200
    assert post(b, "/api/v3/enroll", {"code": second.code, "display_name": "B"}).status_code == 200
    kept = post(b, "/api/v3/devices/pair", {"label": "spare"}).json()["code"]
    other_principal = b.client.get("/api/v3/me").json()["principal"]["id"]
    legacy = root.web_auth.invite("spare", principal_id=other_principal).code

    # A recovers on a new device. B's session and B's prepared code are none of A's business.
    own = post(a, "/api/v3/recovery-code", {}).json()["code"]
    assert post(new_device, "/api/v3/recover", {"code": own}).status_code == 200
    assert a.client.get("/api/v3/me").status_code == 401
    assert b.client.get("/api/v3/me").status_code == 200
    assert post(third_device, "/api/pair", {"code": kept}).status_code == 200
    assert post(third_device, "/api/pair", {"code": legacy}).status_code == 200


@pytest.mark.parametrize("recover_issuer", [False, True])
def test_recovery_voids_legacy_pairings_for_the_target_or_the_issuer(browsers, recover_issuer):
    root, web = browsers
    project = root.identities.create_project("owner", "Shared")
    invited = root.identities.create_member_invite("owner", project.id)
    member = root.identities.redeem_member(invited.code, label="Sam").principal.id
    pairing = root.web_auth.invite("legacy device", principal_id=member, issued_by="owner")
    recovery_actor = "owner" if recover_issuer else member
    recovery = root.identities.issue_recovery(recovery_actor)

    assert post(web["laptop"], "/api/v3/recover", {"code": recovery}).status_code == 200
    assert post(web["thief"], "/api/pair", {"code": pairing.code}).status_code == 403
    assert web["thief"].client.get("/api/v3/me").status_code == 401


def test_recovery_of_an_administrator_also_voids_prepared_node_codes(browsers):
    root, web = browsers
    owner, other = web["phone"], web["laptop"]
    owner.pair()
    recovery = post(owner, "/api/v3/recovery-code", {}).json()["code"]
    node_code = post(owner, "/api/v3/node-invites", {"space_ids": [OWNER_SPACE]}).json()["code"]

    assert post(other, "/api/v3/recover", {"code": recovery}).status_code == 200

    assert owner.client.get("/api/v3/me").status_code == 401
    assert other.client.get("/api/v3/me").status_code == 200
    assert root.nodes.redeem(node_code, ["isolated_exec"]) is None
