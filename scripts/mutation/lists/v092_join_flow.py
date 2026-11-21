"""Mutations for 0.9.2: sharing a project with another person, from the link to the first message.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/v092_join_flow.py
Format and meaning: scripts/mutation/run.py.

Every entry breaks one line that makes the join work: what an invitation lives for, what joining
tells the page, what looking at an invitation costs, and the two places where the project's own
chat had to be made to look like the page the person already has. The listed tests must then
fail. Names start with the area (`invite:`, `join:`, `chat:`).
"""

# ruff: noqa

# Left out on purpose, because no caller can tell the mutation from the original:
# - the `manage=True` of the route that takes invitations back: `revoke_invites` asks the store
#   for the same right, and refuses with the same 403.
# - the project check in `_invited_project`: `create_member_invite` never makes an invitation for
#   a personal space, so there is none to refuse.

ID = "src/personal_agent/collaboration/store.py"
WEB = "src/personal_agent/channels/web/collaboration.py"
API = "src/personal_agent/channels/web/api.py"
RUN = "src/personal_agent/collaboration/runtime.py"
CON = "src/personal_agent/channels/web/contracts.py"

TJ = ["tests/test_v3_join_flow.py"]
TI = ["tests/test_collaboration_identity.py"]
TA = ["tests/test_assistant_http.py"]

M = [
    # ---- what an invitation lives for ----------------------------------------------------
    (
        "invite: a link sent to another person lasts a day, not ten minutes",
        ID,
        "lifetime_s=MEMBER_INVITE_SECONDS,",
        "lifetime_s=INVITE_SECONDS,",
        TJ + TI,
    ),
    (
        "invite: taking invitations back counts only the ones that could still be used",
        ID,
        'AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?",\n'
        "                    (now, space_id, kind, kind, now),",
        'AND used_at IS NULL AND revoked_at IS NULL AND expires_at < ?",\n'
        "                    (now, space_id, kind, kind, now),",
        TJ,
    ),
    (
        "invite: taking invitations back leaves the other kinds of code alone",
        ID,
        "(? IS NULL OR kind = ?) ",
        "(? IS NULL OR kind != ?) ",
        TJ,
    ),
    # ---- what joining tells the page ---------------------------------------------------------
    (
        "join: someone already in the project does not use the invitation up",
        ID,
        'if existing is not None and existing["revoked_at"] is None:',
        'if False and existing is not None and existing["revoked_at"] is None:',
        TJ + TI,
    ),
    (
        "join: the answer says whether anyone was joined",
        ID,
        'return Enrollment(principal, space, False, None, _role(existing["role"]), False)',
        'return Enrollment(principal, space, False, None, _role(existing["role"]), True)',
        TJ,
    ),
    (
        "join: the answer says whether anyone was joined (the page's copy of it)",
        WEB,
        '"joined": enrolled.joined,',
        '"joined": True,',
        TJ,
    ),
    (
        "join: someone already signed in keeps the one session they have",
        WEB,
        "            if existing is not None:\n                # Already signed in as this person",
        "            if False:\n                # Already signed in as this person",
        TJ + TA,
    ),
    # ---- looking at an invitation ----------------------------------------------------------
    (
        "join: reading an invitation does not use it up",
        ID,
        "        space = self._invited_project(row)\n        inside = (",
        "        space = self._invited_project(row)\n        self._consume(row)\n        inside = (",
        TJ,
    ),
    (
        "join: the preview says who is already in the project",
        ID,
        "            principal_id is not None\n            and self.db.one(",
        "            False\n            and self.db.one(",
        TJ,
    ),
    (
        "join: the preview names the person who sent the invitation",
        ID,
        'self.principal(row["issued_by"]).display_name,\n            float(row["expires_at"]),',
        '"",\n            float(row["expires_at"]),',
        TJ,
    ),
    (
        "join: the preview needs no sign-in",
        CON,
        '        InvitePreviewResponse,\n        authority="public",',
        '        InvitePreviewResponse,\n        authority="session",',
        TJ,
    ),
    (
        "join: a right look at an invitation costs nothing",
        WEB,
        "self.throttle(request, count=False)",
        "self.throttle(request)",
        TJ,
    ),
    (
        "join: a wrong code costs an attempt, even when only looking",
        WEB,
        "        except InvalidInvite:\n            self.strike(request)\n            raise",
        "        except InvalidInvite:\n            raise",
        TJ,
    ),
    # ---- the project's chat is the page the person already has -------------------------------
    (
        "chat: a project's chat reports the version of the page it was served with",
        WEB,
        "assets=(self.api.assets, self.api.version),",
        "assets=None,",
        TJ,
    ),
    (
        "chat: a project's chat uses the files it was handed, not its own copy",
        API,
        "assets or load_assets(agent=app.config.agent.name)",
        "load_assets(agent=app.config.agent.name)",
        TJ,
    ),
    (
        "chat: a personal space keeps the product's name",
        RUN,
        'if space.kind == "project":',
        "if True:",
        TJ,
    ),
]
