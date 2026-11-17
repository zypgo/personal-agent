"""Mutations for 0.3.0: identities and spaces, durable tasks, owner-run nodes, deployment, backups.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/v03_collaboration_execution.py
Format and meaning: scripts/mutation/run.py.

Every entry breaks one line of protective code that arrived with 0.3.0. The listed tests must
then fail; one that does not means nothing pins the protection the entry names. Names start with
the area (`identity:`, `execution:`, `files:`, `docker:`, `node:`, `http:`, `space:`, `deploy:`,
`backup:`, `channel:`, `guard:`), so one area can be run alone: `run.py LIST "backup:"`.
"""

# ruff: noqa

# Left out on purpose, because no caller can tell the mutation from the original:
# - the row count check in `_consume`: every redeem path holds the write transaction between
#   `_live_invite` and `_consume`, so nobody else can spend the code in between.
# - the personal-space owner check in `require_access`: a personal space only ever has its owner's
#   membership, and `_require_project` / `create_member_invite` refuse to add anyone else.
# - the "must be a project" check in `redeem_member`: `create_member_invite` already refuses to
#   make an invitation for a personal space.
# - `principal.id != issued_by` in `redeem_device`, `recover` and `bind_channel`: those invitations
#   are only ever made with the issuer as their target.
# - `is_absolute()` in both `relative_path`s: a leading "/" already produces an empty path part,
#   which the next check refuses.
# - the `credential_version` bump in `NodeRegistry.revoke`: a revoked node can no longer
#   authenticate, so the version pinned in its attempts is never compared again.
# - execution: the revision check in `approve_job`, the "asked to cancel" check there and in
#   `claim`, the revision check in `claim` and the current-attempt check in `_attempt`. A task is
#   revised or cancelled by cancelling its jobs first, and a job never has more than one attempt,
#   so the state check next to each of them refuses the same request (with another message).
# - identity: the `manage` and `write` flags of `_live_invite`, the second version check in
#   `bind_channel` and the owner count in `_keep_owner`. An invitation is pinned to its issuer's
#   membership version, which every role change moves, and `_refresh_project_owner` refuses a
#   project without an owner with the same message.
# - http: the `viewer` filter in `node_claim` and the grant, approver and writer conditions of
#   `node_authorized` for work that already runs. `jobs.authorized` compares the whole pinned
#   authorization of the person who asked, a node's credential version moves with every change of
#   its grants, and an attempt only ever starts for work approved by "owner" or by the node's
#   owner, which does not change afterwards: each is the same condition in a weaker form.
# - channel: the membership and identity versions of a queued reply against its binding. Both are
#   compared with the live ones, for the reply and then for the binding, before that point.


def L(*lines: str) -> str:
    return "\n".join(lines)


ID = "src/personal_agent/collaboration/store.py"
CH = "src/personal_agent/collaboration/channel_routing.py"
SC = "src/personal_agent/collaboration/scopes.py"
SN = "src/personal_agent/collaboration/backup.py"
EX = "src/personal_agent/execution/store.py"
EM = "src/personal_agent/execution/models.py"
TR = "src/personal_agent/nodes/transfers.py"
BK = "src/personal_agent/nodes/backend.py"
RG = "src/personal_agent/nodes/registry.py"
WC = "src/personal_agent/channels/web/collaboration.py"
CF = "src/personal_agent/config.py"
AP = "src/personal_agent/app.py"
DP = "src/personal_agent/deploy.py"
GD = "src/personal_agent/tools/guard.py"

# The tests that are meant to pin each area, the most relevant file first so that a killed
# mutation stops early. A mutation that survives these is run once more against every V3 test
# (and the guard, harness and channel tests) before it counts as unpinned.
IDENTITY = [
    "tests/test_v3_identity_pins.py",
    "tests/test_collaboration_identity.py",
    "tests/test_v3_authorization_boundaries.py",
    "tests/test_v3_collaboration_http.py",
    "tests/test_v3_member_reentry_cli.py",
]
RECOVERY = ["tests/test_v3_recovery.py", "tests/test_collaboration_identity.py"]
EXECUTION = [
    "tests/test_v3_execution_fences.py",
    "tests/test_execution_v3.py",
    "tests/test_v3_execution_http.py",
]
PLAN_FILES = ["tests/test_v3_plan_validation.py", "tests/test_execution_v3.py"]
NODE_FILES = ["tests/test_v3_plan_validation.py", "tests/test_v3_nodes.py"]
DOCKER = ["tests/test_v3_docker_backend.py", "tests/test_v3_nodes.py"]
NODE_REGISTRY = [
    "tests/test_v3_node_invitations.py",
    "tests/test_v3_nodes.py",
    "tests/test_v3_execution_http.py",
]
HTTP = [
    "tests/test_v3_http_boundaries.py",
    "tests/test_v3_execution_http.py",
    "tests/test_v3_authorization_boundaries.py",
    "tests/test_v3_collaboration_http.py",
    "tests/test_v3_nodes.py",
]
SPACE = [
    "tests/test_v3_space_config.py",
    "tests/test_collaboration_identity.py",
    "tests/test_v3_runtime.py",
    "tests/test_v3_authorization_boundaries.py",
]
DEPLOY = ["tests/test_v3_control_plane.py", "tests/test_v3_deploy.py"]
BACKUP = ["tests/test_v3_restore_fence.py", "tests/test_v3_backup.py"]
CHANNELS = [
    "tests/test_v3_channel_routing.py",
    "tests/test_v3_channel_identity.py",
    "tests/test_v3_channel_controls.py",
]
GUARD = [
    "tests/test_v3_guard_revocation.py",
    "tests/test_v3_authorization_boundaries.py",
    "tests/test_guard.py",
    "tests/test_v3_runtime.py",
]

# The two `AND 0` spellings below keep the statement valid and the parameter count right, so a
# mutation changes what the statement does and nothing else.
SESSIONS_OF_RECOVERED = L(
    '                "UPDATE web_sessions SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",',
    "                (now, principal.id),",
)
INVITES_OF_RECOVERED = L(
    "                \"UPDATE collaboration_invites SET revoked_at = ? WHERE issued_by = ? AND kind != 'recovery' AND used_at IS NULL AND revoked_at IS NULL\",",
    "                (now, principal.id),",
)
SESSIONS_OF_DISABLED = L(
    '                "UPDATE web_sessions SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",',
    "                (self.clock.now(), target_id),",
)
REQUIRE_ACCESS_DENY = (
    "        if (write and not membership.can_write) or (manage and not membership.can_manage):"
)
APPROVER_CHECK = L(
    "                    allowed_approver_ids is not None",
    '                    and job["approved_by"] not in allowed_approver_ids',
)
CANCEL_AUTH = L(
    '            if task["actor_id"] != actor_id and not authorized:',
    "                raise ExecutionError(",
    '                    "Only the requester or an authorized owner can cancel this task."',
)
PLAN_REVISION_CHECK = (
    '            if job["plan_hash"] != plan_hash or task["revision"] != job["task_revision"]:'
)
APPROVE_EXPIRY = (
    '            if job["desired_state"] == "cancel" or job["deadline_at"] <= self.clock.now():'
)
LEGACY_NODE_GRANT = L(
    "            self.nodes.authorize(node.id, app.space_id)",
    '            and attempt["node_version"] == node.credential_version',
)
RELATIVE_PATH_NAMES = '        p in {".git", ".ssh", ".aws", ".gnupg", ".pa-jobs"} or p == ".env" or p.startswith(".env.")'
TRANSFER_PATH_NAMES = L(
    '        part in {".git", ".ssh", ".aws", ".gnupg", ".pa-jobs"}',
    '        or part == ".env"',
    '        or part.startswith(".env.")',
)

M = [
    # ---- recovery: spending the code ends everything that existed before it ----
    (
        "identity: recovery leaves earlier sessions signed in",
        ID,
        SESSIONS_OF_RECOVERED,
        SESSIONS_OF_RECOVERED.replace('IS NULL",', 'IS NULL AND 0",'),
        RECOVERY,
    ),
    (
        "identity: recovery leaves prepared invitations valid",
        ID,
        INVITES_OF_RECOVERED,
        INVITES_OF_RECOVERED.replace('IS NULL",', 'IS NULL AND 0",'),
        RECOVERY,
    ),
    (
        "identity: recovery leaves prepared node codes valid",
        ID,
        '                "UPDATE node_pairings SET used_at = ? WHERE owner_id = ? AND used_at IS NULL",',
        '                "UPDATE node_pairings SET used_at = ? WHERE owner_id = ? AND used_at IS NULL AND 0",',
        RECOVERY,
    ),
    (
        "identity: a recovery code can be spent twice",
        ID,
        L(
            "            self._consume(row)",
            "            # A recovery code is for the day every device is lost or suspect. Whatever was signed",
        ),
        L(
            "            pass",
            "            # A recovery code is for the day every device is lost or suspect. Whatever was signed",
        ),
        RECOVERY,
    ),
    # ---- access: who may do what in a space ----
    (
        "identity: a revoked membership still opens the space",
        ID,
        '            "SELECT * FROM memberships WHERE principal_id = ? AND space_id = ? AND revoked_at IS NULL",',
        '            "SELECT * FROM memberships WHERE principal_id = ? AND space_id = ?",',
        IDENTITY,
    ),
    (
        "identity: viewers can write",
        ID,
        REQUIRE_ACCESS_DENY,
        "        if manage and not membership.can_manage:",
        IDENTITY,
    ),
    (
        "identity: members can manage",
        ID,
        REQUIRE_ACCESS_DENY,
        "        if write and not membership.can_write:",
        IDENTITY,
    ),
    (
        "identity: a stale membership version is accepted",
        ID,
        "        if version is not None and membership.version != version:",
        "        if False:",
        IDENTITY,
    ),
    (
        "identity: a stale principal version is accepted",
        ID,
        "            if principal_version is not None and principal.authz_version != principal_version:",
        "            if False:",
        IDENTITY,
    ),
    # ---- invitations ----
    (
        "identity: an expired invitation is accepted",
        ID,
        '            "AND revoked_at IS NULL AND expires_at > ?",',
        '            "AND revoked_at IS NULL AND expires_at > ? - 1e12",',
        IDENTITY,
    ),
    (
        "identity: an invitation outlives its issuer's authorization",
        ID,
        '            if issuer.authz_version != row["issuer_version"]:',
        "            if False:",
        IDENTITY,
    ),
    (
        "identity: an invitation ignores a changed membership",
        ID,
        L(
            "                    manage=True,",
            '                    version=row["membership_version"],',
        ),
        L("                    manage=True,", "                    version=None,"),
        IDENTITY,
    ),
    # ---- owners, roles and removal ----
    (
        "identity: a role change does not bump the membership version",
        ID,
        '                "UPDATE memberships SET role = ?, version = version + 1 WHERE space_id = ? AND principal_id = ?",',
        '                "UPDATE memberships SET role = ? WHERE space_id = ? AND principal_id = ?",',
        IDENTITY,
    ),
    (
        "identity: removing a member does not bump the membership version",
        ID,
        '                "UPDATE memberships SET revoked_at = ?, version = version + 1 WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL",',
        '                "UPDATE memberships SET revoked_at = ? WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL",',
        IDENTITY,
    ),
    (
        "identity: removing a member keeps their resource grants",
        ID,
        '                    "UPDATE resource_grants SET revoked_at = ?, version = version + 1 WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL",',
        '                    "UPDATE resource_grants SET revoked_at = ?, version = version + 1 WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL AND 0",',
        IDENTITY,
    ),
    (
        "identity: rejoining keeps the old membership version",
        ID,
        '                    "UPDATE memberships SET role = ?, version = version + 1, revoked_at = NULL, joined_at = ? WHERE space_id = ? AND principal_id = ?",',
        '                    "UPDATE memberships SET role = ?, revoked_at = NULL, joined_at = ? WHERE space_id = ? AND principal_id = ?",',
        IDENTITY,
    ),
    (
        "identity: disabling a person leaves their sessions signed in",
        ID,
        SESSIONS_OF_DISABLED,
        SESSIONS_OF_DISABLED.replace('IS NULL",', 'IS NULL AND 0",'),
        IDENTITY,
    ),
    (
        "identity: disabling a person does not bump their authorization version",
        ID,
        '                "UPDATE principals SET disabled_at = ?, authz_version = authz_version + 1 WHERE id = ? AND disabled_at IS NULL",',
        '                "UPDATE principals SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",',
        IDENTITY,
    ),
    # ---- resource grants ----
    (
        "identity: a viewer uses a resource grant",
        ID,
        L(
            "        self.require_access(principal_id, space_id, write=True)",
            "        rows = self.db.query(",
        ),
        L(
            "        self.require_access(principal_id, space_id, write=False)",
            "        rows = self.db.query(",
        ),
        IDENTITY,
    ),
    (
        "identity: an expired resource grant is honoured",
        ID,
        '            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) ORDER BY principal_id DESC",',
        '            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ? - 1e12) ORDER BY principal_id DESC",',
        IDENTITY,
    ),
    (
        "identity: a stale resource grant version is accepted",
        ID,
        "            if action in grant.actions and (version is None or grant.version == version):",
        "            if action in grant.actions:",
        IDENTITY,
    ),
    # ---- approving a job ----
    (
        "execution: anyone can approve a job",
        EX,
        '            if actor_id != job["approver_id"]:',
        "            if False:",
        EXECUTION,
    ),
    (
        "execution: a job is approved without checking the plan it shows",
        EX,
        PLAN_REVISION_CHECK,
        '            if task["revision"] != job["task_revision"]:',
        EXECUTION,
    ),
    (
        "execution: an expired job can still be approved",
        EX,
        APPROVE_EXPIRY,
        '            if job["desired_state"] == "cancel":',
        EXECUTION,
    ),
    (
        "execution: approval ignores a changed task authorization",
        EX,
        L(
            '            if not self.authorized(task["id"]):',
            '                raise ExecutionConflict("The task authorization changed; create a new plan.")',
        ),
        L(
            "            if False:",
            '                raise ExecutionConflict("The task authorization changed; create a new plan.")',
        ),
        EXECUTION,
    ),
    (
        "execution: a rejoined member revives approved work",
        EX,
        '            return bool(saved == self.authorization_snapshot(task["actor_id"]))',
        "            return True",
        EXECUTION,
    ),
    (
        "execution: work saved with an authorization is trusted when none is configured",
        EX,
        "            return bool(saved == {})",
        "            return True",
        EXECUTION,
    ),
    # ---- a node claims work ----
    (
        "execution: a job that awaits approval is claimed",
        EX,
        "WHERE j.state='queued' AND j.desired_state='run'",
        "WHERE j.state IN ('queued','awaiting_approval') AND j.desired_state='run'",
        EXECUTION,
    ),
    (
        "execution: a task waiting for an answer is claimed",
        EX,
        "AND t.state!='waiting_input' AND j.deadline_at>?",
        "AND j.deadline_at>?",
        EXECUTION,
    ),
    (
        "execution: an expired job is claimed",
        EX,
        "AND j.deadline_at>? ORDER BY",
        "AND j.deadline_at>? - 1e12 ORDER BY",
        EXECUTION,
    ),
    (
        "execution: work of a revoked actor is claimed",
        EX,
        L(
            '                if not self.authorized(job["task_id"]):',
            "                    continue",
        ),
        L("                if False:", "                    continue"),
        EXECUTION,
    ),
    (
        "execution: a node claims work of an actor it is not granted",
        EX,
        '                if allowed_actor_ids is not None and job["actor_id"] not in allowed_actor_ids:',
        "                if False:",
        EXECUTION,
    ),
    (
        "execution: a node claims work approved by someone else",
        EX,
        APPROVER_CHECK,
        APPROVER_CHECK.replace("allowed_approver_ids is not None", "False", 1),
        EXECUTION,
    ),
    (
        "execution: a step runs before the previous step succeeded",
        EX,
        '                if previous is not None and previous["state"] != "succeeded":',
        "                if False:",
        EXECUTION,
    ),
    (
        "execution: an uncertain attempt frees its node",
        EX,
        "AND state IN ('submitting','admitted','running','collecting','uncertain') ORDER BY last_seen_at LIMIT 1",
        "AND state IN ('submitting','admitted','running','collecting') ORDER BY last_seen_at LIMIT 1",
        EXECUTION,
    ),
    (
        "execution: a lost claim answer starts a second attempt on the node",
        EX,
        L("            if current:", '                current_job = self._job(current["job_id"])'),
        L("            if False:", '                current_job = self._job(current["job_id"])'),
        EXECUTION,
    ),
    (
        "execution: a changed node grant does not stop its running work",
        EX,
        '                    or current["node_version"] != node_version',
        "                    or False",
        EXECUTION,
    ),
    # ---- what a node may report ----
    (
        "execution: another node's report is accepted",
        EX,
        '            or row["node_id"] != node_id',
        "            or False",
        EXECUTION,
    ),
    (
        "execution: a stale generation's report is accepted",
        EX,
        L(
            '            row["generation"] != generation',
            '            or row["node_id"] != node_id',
        ),
        L("            False", '            or row["node_id"] != node_id'),
        EXECUTION,
    ),
    (
        "execution: admitting accepts a different plan",
        EX,
        '            if plan_hash != job["plan_hash"]:',
        "            if False:",
        EXECUTION,
    ),
    (
        "execution: an attempt is admitted under a second resource",
        EX,
        '            if attempt["resource_id"] and attempt["resource_id"] != resource_id:',
        "            if False:",
        EXECUTION,
    ),
    (
        "execution: output is observed before the resource was admitted",
        EX,
        '            if state != "uncertain" and not attempt["resource_id"]:',
        "            if False:",
        EXECUTION,
    ),
    (
        "execution: a late heartbeat moves collecting back to running",
        EX,
        '                if state == "uncertain" or ranks[state] >= ranks[attempt["state"]]',
        "                if True",
        EXECUTION,
    ),
    (
        "execution: an old log snapshot is stored again",
        EX,
        '            if sequence is not None and sequence <= attempt["log_sequence"]:',
        "            if False:",
        EXECUTION,
    ),
    (
        "execution: a reused log sequence with other content is accepted",
        EX,
        '                if sequence == attempt["log_sequence"] and log_hash != attempt["log_hash"]:',
        "                if False:",
        EXECUTION,
    ),
    (
        "execution: success is accepted without a zero exit code",
        EX,
        '        if status == "succeeded" and exit_code != 0:',
        "        if False:",
        EXECUTION,
    ),
    (
        "execution: a node returns a file the approved plan never named",
        EX,
        '            if not {item["path"] for item in files}.issubset(plan["output_paths"]):',
        "            if False:",
        EXECUTION,
    ),
    (
        "execution: a second, different final result replaces the first",
        EX,
        '                if attempt["result_hash"] != result_hash:',
        "                if False:",
        EXECUTION,
    ),
    (
        "execution: an attempt that was never admitted completes",
        EX,
        '            if not attempt["resource_id"] and not (',
        "            if False and not (",
        EXECUTION,
    ),
    (
        "execution: a result for a revoked actor is delivered",
        EX,
        '            deliver = deliver and self.authorized(task["id"])',
        "            deliver = deliver",
        EXECUTION,
    ),
    (
        "execution: a result for a superseded revision is delivered",
        EX,
        '                        channel=task["channel"] if deliver and not stale else "web",',
        '                        channel=task["channel"] if deliver else "web",',
        EXECUTION,
    ),
    (
        "execution: a result that must not be sent stays queued for sending",
        EX,
        "                    if (not deliver or stale) and publication.artifact.outbox_id is not None:",
        "                    if False:",
        EXECUTION,
    ),
    (
        "execution: a failed step leaves the later steps queued",
        EX,
        L(
            '                if status != "succeeded":',
            "                    self._stop_successors(job)",
        ),
        L("                if False:", "                    self._stop_successors(job)"),
        EXECUTION,
    ),
    (
        "execution: an active attempt can be discarded",
        EX,
        L(
            '            if attempt["state"] not in TERMINAL:',
            '                raise ExecutionConflict("An active or uncertain attempt cannot be discarded.")',
        ),
        L(
            "            if False:",
            '                raise ExecutionConflict("An active or uncertain attempt cannot be discarded.")',
        ),
        EXECUTION,
    ),
    # ---- cancelling, revising, revoking ----
    (
        "execution: anyone can cancel someone else's task",
        EX,
        CANCEL_AUTH,
        CANCEL_AUTH.replace('if task["actor_id"] != actor_id and not authorized:', "if False:"),
        EXECUTION,
    ),
    (
        "execution: a task that is still running is reported cancelled",
        EX,
        '                    ("cancel_requested" if active else "cancelled", now, task_id),',
        '                    ("cancelled", now, task_id),',
        EXECUTION,
    ),
    (
        "execution: a stale task revision is revised",
        EX,
        L(
            '            if task["revision"] != expected_revision:',
            '                raise ExecutionConflict("Task changed; reload before editing.")',
        ),
        L(
            "            if False:",
            '                raise ExecutionConflict("Task changed; reload before editing.")',
        ),
        EXECUTION,
    ),
    (
        "execution: revoking a person leaves their tasks running",
        EX,
        '            self.cancel_task(task["id"], actor_id=actor_id)',
        "            pass",
        EXECUTION,
    ),
    (
        "execution: work of a revoked actor survives reconciliation",
        EX,
        L(
            '                if not self.authorized(task["id"]):',
            '                    self.cancel_task(task["id"], actor_id=self._task(task["id"])["actor_id"])',
        ),
        L(
            "                if False:",
            '                    self.cancel_task(task["id"], actor_id=self._task(task["id"])["actor_id"])',
        ),
        EXECUTION,
    ),
    (
        "execution: a silent attempt never becomes uncertain",
        EX,
        "                (now - max(1, stale_after_s),),",
        "                (now - 10**9,),",
        EXECUTION,
    ),
    (
        "execution: an unapproved job never expires",
        EX,
        "WHERE state IN ('awaiting_approval','queued') AND deadline_at<=?\",\n                (now, now),",
        "WHERE state IN ('awaiting_approval','queued') AND deadline_at<=? AND 0\",\n                (now, now),",
        EXECUTION,
    ),
    # ---- the plan and the files that travel with it ----
    (
        "files: a path may climb out of the workspace (control plane)",
        EM,
        '    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):',
        '    if path.is_absolute() or any(p in {"", "."} for p in value.split("/")):',
        PLAN_FILES,
    ),
    (
        "files: a path may carry a backslash (control plane)",
        EM,
        r'    if not isinstance(value, str) or not value or len(value) > 500 or "\\" in value:',
        "    if not isinstance(value, str) or not value or len(value) > 500:",
        PLAN_FILES,
    ),
    (
        "files: a path may carry control characters (control plane)",
        EM,
        "    if any(ord(c) < 32 or ord(c) == 127 for c in value):",
        "    if False:",
        PLAN_FILES,
    ),
    (
        "files: .git can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace('".git", ', "", 1),
        PLAN_FILES,
    ),
    (
        "files: .ssh can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace('".ssh", ', "", 1),
        PLAN_FILES,
    ),
    (
        "files: .aws can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace('".aws", ', "", 1),
        PLAN_FILES,
    ),
    (
        "files: .gnupg can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace('".gnupg", ', "", 1),
        PLAN_FILES,
    ),
    (
        "files: the job folder can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace(', ".pa-jobs"', "", 1),
        PLAN_FILES,
    ),
    (
        "files: .env can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace(' or p == ".env"', "", 1),
        PLAN_FILES,
    ),
    (
        "files: .env.production can be transferred (control plane)",
        EM,
        RELATIVE_PATH_NAMES,
        RELATIVE_PATH_NAMES.replace(' or p.startswith(".env.")', "", 1),
        PLAN_FILES,
    ),
    (
        "files: too many files in one manifest",
        EM,
        "    if not isinstance(items, list) or len(items) > MAX_FILES:",
        "    if not isinstance(items, list) or len(items) > MAX_FILES * 100:",
        PLAN_FILES,
    ),
    (
        "files: the same path twice in one manifest",
        EM,
        "        if name in seen:",
        "        if False:",
        PLAN_FILES,
    ),
    (
        "files: a file whose size differs from its manifest",
        EM,
        '        if type(item.get("size")) is not int or item["size"] != len(data):',
        "        if False:",
        PLAN_FILES,
    ),
    (
        "files: a file whose hash differs from its manifest",
        EM,
        '        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != item.get("sha256"):',
        "        if len(data) > MAX_FILE_BYTES:",
        PLAN_FILES,
    ),
    (
        "files: a manifest larger than the total limit",
        EM,
        "        if total > MAX_TOTAL_BYTES:",
        "        if total > MAX_TOTAL_BYTES * 100:",
        PLAN_FILES,
    ),
    (
        "files: a plan with a timeout beyond an hour",
        EM,
        "        or not 1 <= timeout_s <= 3600",
        "        or not 1 <= timeout_s <= 360000",
        PLAN_FILES,
    ),
    (
        "files: a plan that asks for the network",
        EM,
        '        "network": "none",',
        '        "network": "host",',
        PLAN_FILES,
    ),
    (
        "files: the same output path twice",
        EM,
        "    if len(set(outputs)) != len(outputs):",
        "    if False:",
        PLAN_FILES,
    ),
    # ---- the same files on the node: the node never trusts what it is sent ----
    (
        "files: a path may climb out of the workspace (node)",
        TR,
        '        or any(part in {"", ".", ".."} for part in value.split("/"))',
        '        or any(part in {"", "."} for part in value.split("/"))',
        NODE_FILES,
    ),
    (
        "files: a path may carry a backslash (node)",
        TR,
        L(
            r'        "\\" in value',
            "        or any(ord(char) < 32 or ord(char) == 127 for char in value)",
        ),
        L("        False", "        or any(ord(char) < 32 or ord(char) == 127 for char in value)"),
        NODE_FILES,
    ),
    (
        "files: a path may carry control characters (node)",
        TR,
        "        or any(ord(char) < 32 or ord(char) == 127 for char in value)",
        "        or False",
        NODE_FILES,
    ),
    (
        "files: .ssh can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace('".ssh", ', "", 1),
        NODE_FILES,
    ),
    (
        "files: .aws can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace('".aws", ', "", 1),
        NODE_FILES,
    ),
    (
        "files: .gnupg can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace('".gnupg", ', "", 1),
        NODE_FILES,
    ),
    (
        "files: .git can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace('".git", ', "", 1),
        NODE_FILES,
    ),
    (
        "files: the job folder can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace(', ".pa-jobs"', "", 1),
        NODE_FILES,
    ),
    (
        "files: .env can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace('        or part == ".env"\n', "", 1),
        NODE_FILES,
    ),
    (
        "files: .env.production can be transferred (node)",
        TR,
        TRANSFER_PATH_NAMES,
        TRANSFER_PATH_NAMES.replace('\n        or part.startswith(".env.")', "", 1),
        NODE_FILES,
    ),
    (
        "files: the node accepts a plan that asks for the network",
        TR,
        '    if payload.get("network", "none") != "none":',
        "    if False:",
        NODE_FILES,
    ),
    (
        "files: the node accepts a timeout beyond an hour",
        TR,
        "        or not 1 <= timeout <= 3600",
        "        or not 1 <= timeout <= 3600000",
        NODE_FILES,
    ),
    (
        "files: the node accepts the same input twice",
        TR,
        L("        if parts in seen:", '            raise ValueError("duplicate input file")'),
        L("        if False:", '            raise ValueError("duplicate input file")'),
        NODE_FILES,
    ),
    (
        "files: the node accepts an input that differs from its manifest",
        TR,
        '        if len(raw) != size or hashlib.sha256(raw).hexdigest() != entry.get("sha256"):',
        "        if False:",
        NODE_FILES,
    ),
    (
        "files: the node accepts more input than its quota",
        TR,
        L(
            "        if total > MAX_TRANSFER_BYTES:",
            '            raise ValueError("input transfer exceeds the quota")',
        ),
        L("        if False:", '            raise ValueError("input transfer exceeds the quota")'),
        NODE_FILES,
    ),
    (
        "files: the node returns more output than its quota",
        TR,
        L(
            "        if total > MAX_TRANSFER_BYTES:",
            '            raise ValueError("output transfer exceeds the quota")',
        ),
        L("        if False:", '            raise ValueError("output transfer exceeds the quota")'),
        NODE_FILES,
    ),
    (
        "files: a workspace of a different plan is reused",
        TR,
        '        if json.loads(marker.read_text()).get("plan_hash") != payload["plan_hash"]:',
        "        if False:",
        NODE_FILES,
    ),
    (
        "files: a workspace that is a link is reused",
        TR,
        "        if workspace.is_symlink() or not marker.is_file() or marker.is_symlink():",
        "        if not marker.is_file() or marker.is_symlink():",
        NODE_FILES,
    ),
    (
        "files: a workspace without its marker is reused",
        TR,
        "        if workspace.is_symlink() or not marker.is_file() or marker.is_symlink():",
        "        if workspace.is_symlink() or marker.is_symlink():",
        NODE_FILES,
    ),
    (
        "files: a workspace whose marker is a link is reused",
        TR,
        "        if workspace.is_symlink() or not marker.is_file() or marker.is_symlink():",
        "        if workspace.is_symlink() or not marker.is_file():",
        NODE_FILES,
    ),
    (
        "files: an output that is a link is followed",
        TR,
        "        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)",
        "        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK, dir_fd=directory)",
        NODE_FILES,
    ),
    (
        "files: a folder on the way to an output that is a link is followed",
        TR,
        "            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)",
        "            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory)",
        NODE_FILES,
    ),
    (
        "files: a workspace that is a link is followed",
        TR,
        "    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)",
        "    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)",
        NODE_FILES,
    ),
    (
        "files: an output with a second hard link is collected",
        TR,
        "                or before.st_nlink != 1",
        "                or False",
        NODE_FILES,
    ),
    (
        "files: an output larger than a file may be is collected",
        TR,
        "                or before.st_size > MAX_FILE_BYTES",
        "                or False",
        NODE_FILES,
    ),
    (
        "files: an output that changed while it was read is collected",
        TR,
        "                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)",
        "                != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)",
        NODE_FILES,
    ),
    # ---- the container a node starts ----
    (
        "docker: the container has network access",
        BK,
        L('            "--network",', '            "none",'),
        L('            "--network",', '            "bridge",'),
        DOCKER,
    ),
    (
        "docker: the container root is writable",
        BK,
        L('            "--read-only",', '            "--cap-drop",'),
        L('            "--cap-drop",'),
        DOCKER,
    ),
    (
        "docker: the container keeps its capabilities",
        BK,
        L('            "--cap-drop",', '            "ALL",'),
        L('            "--cap-add",', '            "ALL",'),
        DOCKER,
    ),
    (
        "docker: the container can gain privileges",
        BK,
        L('            "--security-opt",', '            "no-new-privileges",'),
        L('            "--security-opt",', '            "seccomp=unconfined",'),
        DOCKER,
    ),
    (
        "docker: the container has no process limit",
        BK,
        L('            "--pids-limit",', '            "128",'),
        L('            "--pids-limit",', '            "-1",'),
        DOCKER,
    ),
    (
        "docker: the container can use swap",
        BK,
        L('            "--memory-swap",', '            f"{self.memory_mb}m",'),
        L('            "--memory-swap",', '            "-1",'),
        DOCKER,
    ),
    (
        "docker: the container has no memory limit",
        BK,
        L('            "--memory",', '            f"{self.memory_mb}m",'),
        L('            "--memory",', '            "0",'),
        DOCKER,
    ),
    (
        "docker: the container has no cpu limit",
        BK,
        L('            "--cpus",', "            str(self.cpus),"),
        L('            "--cpus",', '            "0",'),
        DOCKER,
    ),
    (
        "docker: a process may write files of any size",
        BK,
        L('            "--ulimit",', '            "fsize=67108864:67108864",'),
        L('            "--ulimit",', '            "fsize=-1:-1",'),
        DOCKER,
    ),
    (
        "docker: a process may open any number of files",
        BK,
        L('            "--ulimit",', '            "nofile=256:256",'),
        L('            "--ulimit",', '            "nofile=1048576:1048576",'),
        DOCKER,
    ),
    (
        "docker: the scratch folder is huge and runs programs",
        BK,
        '            "/tmp:rw,nosuid,size=128m",  # noqa: S108',
        '            "/tmp:rw,size=128g",  # noqa: S108',
        DOCKER,
    ),
    (
        "docker: the container runs as root",
        BK,
        L('            "--user",', '            f"{uid}:{gid}",'),
        L('            "--user",', '            "0:0",'),
        DOCKER,
    ),
    (
        "docker: the container has no init process",
        BK,
        L('            "--init",', '            "--network",'),
        L('            "--network",'),
        DOCKER,
    ),
    (
        "docker: an image may be pulled at start",
        BK,
        '            "--pull=never",',
        '            "--pull=always",',
        DOCKER,
    ),
    (
        "docker: a container with someone else's attempt is adopted",
        BK,
        '                labels.get("pa.attempt") != receipt.attempt_id',
        "                False",
        DOCKER,
    ),
    (
        "docker: a container with another plan is adopted",
        BK,
        '                or labels.get("pa.plan") != receipt.plan_hash',
        "                or False",
        DOCKER,
    ),
    (
        "docker: a container of another generation is adopted",
        BK,
        '                or labels.get("pa.generation") != str(receipt.generation)',
        "                or False",
        DOCKER,
    ),
    # ---- node credentials ----
    (
        "node: a revoked token authenticates",
        RG,
        '            "SELECT * FROM nodes WHERE token_hash=? AND revoked_at IS NULL", (token_hash(token),)',
        '            "SELECT * FROM nodes WHERE token_hash=?", (token_hash(token),)',
        NODE_REGISTRY,
    ),
    (
        "node: a node is authorized for a space it was not granted",
        RG,
        "        return bool(node and node.revoked_at is None and space_id in node.space_ids)",
        "        return bool(node and node.revoked_at is None)",
        NODE_REGISTRY,
    ),
    (
        "node: a revoked node is authorized",
        RG,
        "        return bool(node and node.revoked_at is None and space_id in node.space_ids)",
        "        return bool(node and space_id in node.space_ids)",
        NODE_REGISTRY,
    ),
    (
        "node: a pairing code works twice",
        RG,
        '                "SELECT * FROM node_pairings WHERE token_hash=? AND used_at IS NULL AND expires_at>?",',
        '                "SELECT * FROM node_pairings WHERE token_hash=? AND expires_at>?",',
        NODE_REGISTRY,
    ),
    (
        "node: an expired pairing code works",
        RG,
        '                "SELECT * FROM node_pairings WHERE token_hash=? AND used_at IS NULL AND expires_at>?",',
        '                "SELECT * FROM node_pairings WHERE token_hash=? AND used_at IS NULL AND expires_at>? - 1e12",',
        NODE_REGISTRY,
    ),
    (
        "node: another owner revokes a node",
        RG,
        L(
            '                """UPDATE nodes SET revoked_at=?, credential_version=credential_version+1',
            '                   WHERE id=? AND owner_id=? AND revoked_at IS NULL""",',
        ),
        L(
            '                """UPDATE nodes SET revoked_at=?, credential_version=credential_version+1',
            '                   WHERE id=? AND (owner_id=? OR 1) AND revoked_at IS NULL""",',
        ),
        NODE_REGISTRY,
    ),
    (
        "node: changed grants keep the old credential version",
        RG,
        '                """UPDATE nodes SET space_ids=?, capabilities=?, credential_version=credential_version+1',
        '                """UPDATE nodes SET space_ids=?, capabilities=?, credential_version=credential_version',
        NODE_REGISTRY,
    ),
    (
        "node: a node is granted what it never declared",
        RG,
        "        return tuple(sorted(set(self.capabilities) & set(self.declared_capabilities)))",
        "        return tuple(sorted(set(self.capabilities) | set(self.declared_capabilities)))",
        NODE_REGISTRY,
    ),
    (
        "node: unlimited pending pairing invitations",
        RG,
        "            if pending >= 32:",
        "            if pending >= 3200:",
        NODE_REGISTRY,
    ),
    (
        "node: an invitation grants a capability that does not exist",
        RG,
        L(
            "        caps = _strings(capabilities)",
            "        if not set(caps) <= set(DEFAULT_CAPABILITIES):",
        ),
        L("        caps = _strings(capabilities)", "        if False:"),
        NODE_REGISTRY,
    ),
    # ---- the web layer ----
    (
        "http: a signed-in person is not revalidated during a long request",
        WC,
        "            if current is None or current.id != session.id:",
        "            if False:",
        HTTP,
    ),
    (
        "http: the selected space is not checked against the caller's role",
        WC,
        L(
            "        membership = self.identities.require_access(",
            "            session.principal_id, space_id, write=write, manage=manage",
            "        )",
        ),
        L(
            "        membership = self.identities.require_access(",
            "            session.principal_id, space_id, write=False, manage=False",
            "        )",
        ),
        HTTP,
    ),
    (
        "http: a long request ignores a membership that changed",
        WC,
        "                version=membership.version,",
        "                version=None,",
        HTTP,
    ),
    (
        "http: the session cookie can be read by scripts",
        WC,
        L("            httponly=True,", "            secure=self.secure,"),
        L("            httponly=False,", "            secure=self.secure,"),
        HTTP,
    ),
    (
        "http: the session cookie travels with cross-site requests",
        WC,
        L(
            "            secure=self.secure,",
            '            samesite="strict",',
            '            path="/",',
        ),
        L(
            "            secure=self.secure,",
            '            samesite="lax",',
            '            path="/",',
        ),
        HTTP,
    ),
    (
        "http: nothing limits attempts on enrollment, recovery or node pairing",
        WC,
        L(
            "        if self.strikes.wait(source, now) is not None:",
            '            raise HTTPException(429, "Too many attempts; try again later.")',
        ),
        L(
            "        if False:",
            '            raise HTTPException(429, "Too many attempts; try again later.")',
        ),
        HTTP,
    ),
    (
        "http: viewers can write through the legacy routes",
        WC,
        '                write=scope["method"] not in ("GET", "HEAD", "OPTIONS") and path != "/api/logout",',
        "                write=False,",
        HTTP,
    ),
    (
        "http: anyone in a space decides chat approvals",
        WC,
        L(
            '            if path.startswith("/api/approvals/"):',
            "                app.require_approval_actor(session.principal_id)",
        ),
        L('            if path.startswith("/api/approvals/"):', "                pass"),
        HTTP,
    ),
    (
        "http: removing a member leaves their tasks running",
        WC,
        L("            if app.jobs is not None:", "                app.jobs.revoke_actor(target)"),
        L("            if app.jobs is not None:", "                pass"),
        HTTP,
    ),
    (
        "http: anyone signed in mints node pairing codes",
        WC,
        L(
            "        self.identities.require_admin(session.principal_id)",
            "        data = await body(request)",
            '        space_ids = strings(data, "space_ids")',
        ),
        L("        data = await body(request)", '        space_ids = strings(data, "space_ids")'),
        HTTP,
    ),
    (
        "http: anyone signed in revokes nodes",
        WC,
        L(
            "        self.identities.require_admin(session.principal_id)",
            "        return answer(",
            '            {"ok": self.nodes.revoke(request.path_params["node_id"], session.principal_id)}',
        ),
        L(
            "        return answer(",
            '            {"ok": self.nodes.revoke(request.path_params["node_id"], session.principal_id)}',
        ),
        HTTP,
    ),
    (
        "http: a node claims work approved by anyone",
        WC,
        '        approvers = {node.owner_id, "owner"}',
        "        approvers = None",
        HTTP,
    ),
    (
        "http: a node keeps working after its authorization ended",
        WC,
        "                if not self.node_authorized(node, app, attempt_record):",
        "                if False:",
        HTTP,
    ),
    (
        "http: a node reports on another node's attempt",
        WC,
        '        if attempt["node_id"] != node.id:',
        "        if False:",
        HTTP,
    ),
    (
        "http: a node reports for a stale generation",
        WC,
        '        if type(generation) is not int or generation != attempt["generation"]:',
        "        if type(generation) is not int:",
        HTTP,
    ),
    (
        "http: a node reports for a plan that was not approved",
        WC,
        '        if text(data, "plan_hash", maximum=64) != attempt["plan_hash"]:',
        "        if False:",
        HTTP,
    ),
    (
        "http: a node's request does not cancel work it lost authority for",
        WC,
        L(
            "        if not allowed:",
            '            app.jobs.cancel_task(task["id"], actor_id=task["actor_id"])',
        ),
        L(
            "        if False:",
            '            app.jobs.cancel_task(task["id"], actor_id=task["actor_id"])',
        ),
        HTTP,
    ),
    (
        "http: a node admits work after its authorization ended",
        WC,
        L('        if method == "admit":', "            if not allowed:"),
        L('        if method == "admit":', "            if False:"),
        HTTP,
    ),
    (
        "http: a result is delivered although the node was not authorized",
        WC,
        L("                deliver=allowed,", "            )"),
        L("                deliver=True,", "            )"),
        HTTP,
    ),
    (
        "http: a changed node credential version does not stop running work",
        WC,
        LEGACY_NODE_GRANT,
        LEGACY_NODE_GRANT.replace(
            '            and attempt["node_version"] == node.credential_version',
            "            and True",
            1,
        ),
        HTTP,
    ),
    (
        "http: a node runs work needing capabilities it lacks",
        WC,
        '            and set(job["plan"]["required_capabilities"]).issubset(node.effective_capabilities)',
        "            and True",
        HTTP,
    ),
    (
        "http: a node runs work whose task authorization changed",
        WC,
        '            and app.jobs.authorized(task["id"])',
        "            and True",
        HTTP,
    ),
    # ---- spaces: what a new space does not inherit ----
    (
        "space: a space identifier may be anything",
        SC,
        'SPACE_ID = re.compile(r"s_[a-f0-9]{32}\\Z")',
        'SPACE_ID = re.compile(r".+")',
        SPACE,
    ),
    (
        "space: a new space inherits the shell",
        SC,
        '    data["tools"]["shell"] = False',
        '    data["tools"]["shell"] = root.tools.shell',
        SPACE,
    ),
    (
        "space: a new space inherits extra readable folders",
        SC,
        '    data["sandbox"]["extra_read"] = []',
        '    data["sandbox"]["extra_read"] = root.sandbox.extra_read',
        SPACE,
    ),
    (
        "space: a new space inherits extra writable folders",
        SC,
        '    data["sandbox"]["extra_write"] = []',
        '    data["sandbox"]["extra_write"] = root.sandbox.extra_write',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's secrets",
        SC,
        '    data["secrets"] = {}',
        '    data["secrets"] = root.secrets',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's MCP servers",
        SC,
        '    data["mcp"] = {"enabled": False, "servers": []}',
        '    data["mcp"] = root.mcp.model_dump(mode="python")',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's plugins",
        SC,
        '    data["plugins"] = {"enabled": [], "settings": {}}',
        '    data["plugins"] = root.plugins.model_dump(mode="python")',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's skills",
        SC,
        '    data["skills"]["enabled"] = False',
        '    data["skills"]["enabled"] = root.skills.enabled',
        SPACE,
    ),
    (
        "space: a new space inherits access to private networks",
        SC,
        '    data["net"]["allow_private"] = []',
        '    data["net"]["allow_private"] = root.net.allow_private',
        SPACE,
    ),
    (
        "space: a new space acts without asking",
        SC,
        '        "mode": "supervised",',
        '        "mode": "autonomous",',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's Telegram bot",
        SC,
        '    data["telegram"] = {"enabled": False}',
        '    data["telegram"] = root.telegram.model_dump(mode="python")',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's DingTalk bot",
        SC,
        '    data["dingtalk"] = {"enabled": False}',
        '    data["dingtalk"] = root.dingtalk.model_dump(mode="python")',
        SPACE,
    ),
    (
        "space: a new space inherits the owner's webhooks",
        SC,
        '    data["webhooks"] = {"enabled": False, "routes": {}}',
        '    data["webhooks"] = root.webhooks.model_dump(mode="python")',
        SPACE,
    ),
    # ---- the deployed control plane ----
    (
        "deploy: the control plane keeps the shell (build)",
        AP,
        '        cfg = cfg.model_copy(update={"tools": cfg.tools.model_copy(update={"shell": False})})',
        '        cfg = cfg.model_copy(update={"tools": cfg.tools.model_copy(update={"shell": True})})',
        DEPLOY,
    ),
    (
        "deploy: the control plane keeps the shell (environment)",
        CF,
        L("    if control_only:", '        data["tools"]["shell"] = False'),
        L("    if control_only:", "        pass"),
        DEPLOY,
    ),
    (
        "deploy: the control plane listens on the loopback only",
        CF,
        '        data["web"]["host"] = "0.0.0.0"  # noqa: S104 - behind the configured HTTPS proxy',
        '        data["web"]["host"] = "127.0.0.1"',
        DEPLOY,
    ),
    (
        "deploy: a startup without a public https address",
        DP,
        '    if not config.web.enabled or not (config.web.public_url or "").startswith("https://"):',
        "    if not config.web.enabled:",
        DEPLOY,
    ),
    (
        "deploy: a startup with the shell enabled",
        DP,
        L(
            "    if config.tools.shell:",
            '        raise ValueError("The deployed control plane must disable the local shell")',
        ),
        L(
            "    if False:",
            '        raise ValueError("The deployed control plane must disable the local shell")',
        ),
        DEPLOY,
    ),
    (
        "deploy: a startup with Python plugins enabled",
        DP,
        L(
            "    if config.plugins.enabled:",
            '        raise ValueError("Python plugins must be disabled on the deployed control plane")',
        ),
        L(
            "    if False:",
            '        raise ValueError("Python plugins must be disabled on the deployed control plane")',
        ),
        DEPLOY,
    ),
    (
        "deploy: a startup with a stdio MCP server enabled",
        DP,
        '        server.enabled and server.transport == "stdio" for server in config.mcp.servers',
        "        False for server in config.mcp.servers",
        DEPLOY,
    ),
    (
        "deploy: the administrator commands skip the control plane checks",
        DP,
        "        validate_control_config(config)",
        "        pass",
        DEPLOY,
    ),
    (
        "deploy: a data folder owned by someone else is used",
        DP,
        "    elif home.stat().st_uid != os.geteuid():",
        "    elif False:",
        DEPLOY,
    ),
    # ---- snapshots and restore ----
    (
        "backup: a restore keeps the old authorization versions",
        SN,
        '            root.execute("UPDATE principals SET authz_version=authz_version+1")',
        '            root.execute("SELECT 1")',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old browser sessions",
        SN,
        '            root.execute("UPDATE web_sessions SET revoked_at=COALESCE(revoked_at,?)", (now,))',
        '            root.execute("SELECT ?", (now,))',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old pairing links",
        SN,
        '            root.execute("UPDATE web_invites SET used_at=COALESCE(used_at,?)", (now,))',
        '            root.execute("SELECT ?", (now,))',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old invitations",
        SN,
        '            root.execute("UPDATE collaboration_invites SET used_at=COALESCE(used_at,?)", (now,))',
        '            root.execute("SELECT ?", (now,))',
        BACKUP,
    ),
    (
        "backup: a restore keeps nodes authorized",
        SN,
        '                "UPDATE nodes SET revoked_at=COALESCE(revoked_at,?), credential_version=credential_version+1",',
        '                "UPDATE nodes SET credential_version=credential_version+1 WHERE ?>0",',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old node credential version",
        SN,
        '                "UPDATE nodes SET revoked_at=COALESCE(revoked_at,?), credential_version=credential_version+1",',
        '                "UPDATE nodes SET revoked_at=COALESCE(revoked_at,?)",',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old node pairing codes",
        SN,
        '            root.execute("UPDATE node_pairings SET used_at=COALESCE(used_at,?)", (now,))',
        '            root.execute("SELECT ?", (now,))',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old legacy chat owners",
        SN,
        '            root.execute("DELETE FROM legacy_channel_owners")',
        '            root.execute("SELECT 1")',
        BACKUP,
    ),
    (
        "backup: a restore keeps the old chat account bindings",
        SN,
        '                "UPDATE channel_identities SET revoked_at=COALESCE(revoked_at,?), version=version+1",',
        '                "UPDATE channel_identities SET version=version+1 WHERE ?>0",',
        BACKUP,
    ),
    (
        "backup: a restore keeps timers running",
        SN,
        "UPDATE timers SET status='paused' WHERE status='active'",
        "UPDATE timers SET status='paused' WHERE 0",
        BACKUP,
    ),
    (
        "backup: a restore replays queued events",
        SN,
        "lease_expires_at=NULL WHERE status IN ('pending','leased')",
        "lease_expires_at=NULL WHERE 0",
        BACKUP,
    ),
    (
        "backup: a restore sends the queued replies",
        SN,
        "WHERE status IN ('pending','sending')",
        "WHERE 0",
        BACKUP,
    ),
    (
        "backup: a restore writes into a folder that already exists",
        SN,
        "    if target.exists() or target.is_symlink() or target.is_relative_to(source):",
        "    if False:",
        BACKUP,
    ),
    (
        "backup: a restored file that differs from its snapshot is kept",
        SN,
        '            if _hash(dest) != entry["sha256"]:',
        "            if False:",
        BACKUP,
    ),
    # ---- chat accounts routed to a person ----
    (
        "channel: a message from another chat of a bound account is routed",
        CH,
        "        if binding is not None and binding.recipient_id == recipient:",
        "        if binding is not None:",
        CHANNELS,
    ),
    (
        "channel: a bound account is routed into a space it no longer belongs to",
        CH,
        L(
            "                if app.space_id != binding.space_id:",
            "                    return None",
        ),
        L("                if False:", "                    return None"),
        CHANNELS,
    ),
    (
        "channel: the old owner chat keeps working after the account was bound",
        CH,
        "        and not identities.has_channel_binding(channel, account, sender)",
        "        and True",
        CHANNELS,
    ),
    (
        "channel: the old owner chat works after the owner lost access",
        CH,
        "        and identities.authorized(OWNER_ID, OWNER_SPACE, write=True)",
        "        and True",
        CHANNELS,
    ),
    (
        "channel: a reply is sent after the sender lost access",
        CH,
        L("        actor,", "        app.space_id,", "        write=True,"),
        L("        actor,", "        app.space_id,", "        write=False,"),
        CHANNELS,
    ),
    (
        "channel: a reply follows a binding into another space",
        CH,
        "            binding.space_id == app.space_id",
        "            True",
        CHANNELS,
    ),
    (
        "channel: a reply follows an account to another person",
        CH,
        "            and binding.principal_id == actor",
        "            and True",
        CHANNELS,
    ),
    (
        "channel: a reply follows a person to another chat",
        CH,
        "            and binding.recipient_id == recipient",
        "            and True",
        CHANNELS,
    ),
    # ---- a revoked grant must stop a running tool call ----
    (
        "guard: a tool decision is made for a revoked actor",
        GD,
        L("        rc.require_authorized()", "        cached = rc.decisions.get(call_id)"),
        L("        pass", "        cached = rc.decisions.get(call_id)"),
        GUARD,
    ),
    (
        "guard: a tool call is started for a revoked actor",
        GD,
        L(
            "        rc: RunContext = tool_context.context",
            "        rc.require_authorized()",
            "        call_id = tool_context.tool_call_id",
        ),
        L(
            "        rc: RunContext = tool_context.context",
            "        pass",
            "        call_id = tool_context.tool_call_id",
        ),
        GUARD,
    ),
    (
        "guard: a tool runs after a slow review although the grant was revoked",
        GD,
        "        rc.require_authorized()  # A remote policy review may have outlived the actor's grant.",
        "        pass",
        GUARD,
    ),
    (
        "guard: a tool starts running after the grant was revoked while it waited",
        GD,
        L(
            "            # wait_for schedules a separate task. Recheck when that task actually starts.",
            "            rc.require_authorized()",
        ),
        L(
            "            # wait_for schedules a separate task. Recheck when that task actually starts.",
            "            pass",
        ),
        GUARD,
    ),
]
