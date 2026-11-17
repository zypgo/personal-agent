"""Mutations for 0.2.0: file delivery, the DingTalk endpoint, streamed previews, provider endpoints.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/v02_channels_files.py
Format and meaning: scripts/mutation/run.py.

Every entry breaks one line of protective code that arrived with 0.2.0. The listed tests must
then fail; one that does not means nothing pins the protection the entry names.
"""

# ruff: noqa

# Left out on purpose: dropping the "no password in the address" checks (`dt stream`, `ep native`)
# changes nothing. `urlsplit` reports an empty user name whenever there is a password, and the
# "no user name" check right next to it refuses that address first. No test can tell them apart.

AR = "src/personal_agent/core/artifacts.py"
WA = "src/personal_agent/channels/web/artifacts.py"
TA = "src/personal_agent/tools/artifacts.py"
OB = "src/personal_agent/core/outbox.py"
DT = "src/personal_agent/channels/dingtalk/api.py"
LV = "src/personal_agent/core/live.py"
ST = "src/personal_agent/harness/streaming.py"
EP = "src/personal_agent/harness/endpoints.py"
SV = "src/personal_agent/commands/service.py"

FILES = ["tests/test_artifacts.py", "tests/test_artifacts_delivery.py"]
ROUTE = ["tests/test_artifacts_delivery.py"]
POLICY = ["tests/test_artifacts_policy.py", "tests/test_artifacts.py"]
OUTBOX = ["tests/test_outbox.py", "tests/test_artifacts_delivery.py", "tests/test_dispatcher.py"]
DINGTALK = ["tests/test_dingtalk_api.py"]
STREAM = ["tests/test_stream_privacy.py"]
PROVIDER = ["tests/test_provider_config.py"]
SERVICE = ["tests/test_cli_service.py"]

# The published line of a file's name checks, spelled once so each entry shows only what differs.
CAPTION_CHECK = (
    "        if redact(value, known) != value or redact(visible, visible_known) != visible:"
)
BLOB_LISTING = (
    '                if name.endswith(".blob") and ARTIFACT_ID.fullmatch(name[:-5]) '
    "and name not in live:"
)
SIGNATURE_CHECK = (
    "                if _signature(before) != _signature(after) or len(data) != after.st_size:"
)
INFO_CHECK = "        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:"
STREAM_HOST = '        or not host.endswith(".dingtalk.com")'
SEND_RESULT_CHECK = (
    "        if sending and (status == 408 or (200 <= status < 400 and status not in (200, 201))):"
)

M = [
    # ---- opening the source: never through a link, never anything but an ordinary file ----
    (
        "ar open: a symlinked folder is followed",
        AR,
        "_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW",
        "_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY",
        FILES,
    ),
    (
        "ar open: a symlinked file is followed",
        AR,
        "        fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)",
        "        fd = os.open(names[-1], os.O_RDONLY | os.O_NONBLOCK, dir_fd=parent)",
        FILES,
    ),
    (
        "ar open: a pipe or a device is read",
        AR,
        INFO_CHECK,
        "        if info.st_nlink != 1:",
        FILES,
    ),
    (
        "ar open: a hard link is read",
        AR,
        INFO_CHECK,
        "        if not stat.S_ISREG(info.st_mode):",
        FILES,
    ),
    (
        "ar snapshot: a file rewritten while it is read is accepted",
        AR,
        SIGNATURE_CHECK,
        "                if False:",
        FILES,
    ),
    (
        "ar snapshot: only the size is compared after the read",
        AR,
        SIGNATURE_CHECK,
        "                if len(data) != after.st_size:",
        FILES,
    ),
    # ---- the source path as the owner wrote it -------------------------------------------
    (
        "ar path: parent folders are allowed",
        AR,
        '    if ".." in path.parts:',
        "    if False:",
        FILES + POLICY,
    ),
    (
        "ar path: an absolute path outside the workspace is allowed",
        AR,
        "    if path.is_absolute():",
        "    if False:",
        FILES + POLICY,
    ),
    (
        "ar path: the workspace folder itself is named",
        AR,
        "    if not path.parts:",
        "    if False:",
        FILES + POLICY,
    ),
    (
        "ar path: a NUL byte reaches the file system",
        AR,
        '    if not raw or "\\0" in raw or raw.startswith("~"):',
        '    if not raw or raw.startswith("~"):',
        FILES + POLICY,
    ),
    # ---- what may be said about the file -------------------------------------------------
    (
        "ar caption: a credential in a caption is sent",
        AR,
        CAPTION_CHECK,
        "        if False:",
        FILES + POLICY,
    ),
    (
        "ar caption: a credential split by invisible characters is sent",
        AR,
        CAPTION_CHECK,
        "        if redact(value, known) != value:",
        FILES + POLICY,
    ),
    (
        "ar caption: any length",
        AR,
        "    if len(description) > MAX_DESCRIPTION_CHARS:",
        "    if False:",
        FILES + POLICY,
    ),
    (
        "ar name: a folder in the filename",
        AR,
        '        or any(c in value for c in ("/", "\\\\"))',
        "        or False",
        FILES + POLICY,
    ),
    (
        "ar name: control or invisible characters in the filename",
        AR,
        '        or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)',
        "        or False",
        FILES + POLICY,
    ),
    (
        "ar name: an overlong filename",
        AR,
        '        or len(value.encode("utf-8", errors="replace")) > 240',
        "        or False",
        FILES + POLICY,
    ),
    # ---- publishing: limits, retries, what is left behind --------------------------------
    (
        "ar publish: a disabled feature still publishes",
        AR,
        "        if not self.config.enabled:",
        "        if False:",
        FILES + POLICY,
    ),
    (
        "ar publish: no durable task identifier is required",
        AR,
        "        if not work_id or not session_key:",
        "        if False:",
        FILES + POLICY,
    ),
    (
        "ar publish: a retry in the same task publishes again",
        AR,
        "            if existing is not None:\n                return Publication(",
        "            if False:\n                return Publication(",
        FILES,
    ),
    (
        "ar publish: the storage quota is not enforced",
        AR,
        "            if used + len(data) > self.config.max_total_bytes:",
        "            if False:",
        FILES,
    ),
    (
        "ar publish: a file that exactly fills the quota is refused",
        AR,
        "            if used + len(data) > self.config.max_total_bytes:",
        "            if used + len(data) >= self.config.max_total_bytes:",
        FILES,
    ),
    (
        "ar publish: expired snapshots still fill the quota",
        AR,
        '"SELECT COALESCE(SUM(size),0),COUNT(*) FROM artifacts WHERE expires_at>?", (now,)',
        '"SELECT COALESCE(SUM(size),0),COUNT(*) FROM artifacts WHERE expires_at>?", (0.0,)',
        FILES,
    ),
    (
        "ar publish: the active file count is not limited",
        AR,
        "            if count >= self.config.max_active_files:",
        "            if False:",
        FILES,
    ),
    (
        "ar publish: one more file than the count limit is let in",
        AR,
        "            if count >= self.config.max_active_files:",
        "            if count > self.config.max_active_files:",
        FILES,
    ),
    (
        "ar publish: the answer to a chat app has no way to the file",
        AR,
        '                if channel not in ("web", "telegram"):',
        "                if False:",
        FILES,
    ),
    (
        "ar publish: a failed publication leaves its blob behind",
        AR,
        "                if created:",
        "                if False:",
        FILES,
    ),
    # ---- storage: private to the owner, reclaimed on schedule ----------------------------
    (
        "ar store: the folder is open to other accounts",
        AR,
        "        directory.mkdir(mode=0o700, parents=True, exist_ok=True)",
        "        directory.mkdir(mode=0o755, parents=True, exist_ok=True)",
        FILES,
    ),
    (
        "ar store: a blob is open to other accounts",
        AR,
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,\n                    0o600,",
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,\n                    0o644,",
        FILES,
    ),
    (
        "ar read: an expired snapshot is served",
        AR,
        "        if artifact.expires_at <= self.clock.now():",
        "        if False:",
        FILES,
    ),
    (
        "ar read: a snapshot is still served at the instant it expires",
        AR,
        "        if artifact.expires_at <= self.clock.now():",
        "        if artifact.expires_at < self.clock.now():",
        FILES,
    ),
    (
        "ar read: a blob whose bytes changed is served",
        AR,
        "                    or hashlib.sha256(data).hexdigest() != artifact.sha256",
        "                    or False",
        FILES,
    ),
    (
        "ar sweep: a live snapshot is deleted",
        AR,
        "and name not in live:",
        "and name in live:",
        FILES,
    ),
    (
        "ar sweep: any file in the folder is deleted",
        AR,
        BLOB_LISTING,
        "                if name not in live:",
        FILES,
    ),
    (
        "ar sweep: an unlisted blob is kept",
        AR,
        BLOB_LISTING,
        "                if False:",
        FILES,
    ),
    (
        "ar sweep: an expired snapshot is not reclaimed",
        AR,
        '"SELECT id FROM artifacts WHERE expires_at>?", (self.clock.now(),)',
        '"SELECT id FROM artifacts WHERE expires_at>?", (0.0,)',
        FILES,
    ),
    # ---- the download: signed in, saved, never run -------------------------------------------
    (
        "wa download: no sign-in is needed",
        WA,
        "        if session is None:",
        "        if False:",
        ROUTE,
    ),
    (
        "wa download: the file announces its own type",
        WA,
        '            media_type="application/octet-stream",',
        "            media_type=artifact.mime,",
        ROUTE,
    ),
    (
        "wa download: shown in the page instead of saved",
        WA,
        "                    f'attachment; filename=\"{fallback}\"; '",
        "                    f'inline; filename=\"{fallback}\"; '",
        ROUTE,
    ),
    (
        "wa download: the browser may guess the type",
        WA,
        '                "X-Content-Type-Options": "nosniff",',
        '                "X-Content-Type-Options": "sniff",',
        ROUTE,
    ),
    (
        "wa download: the file runs with the page's rights",
        WA,
        '                "Content-Security-Policy": "sandbox; default-src \'none\'",',
        '                "Content-Security-Policy": "default-src \'none\'",',
        ROUTE,
    ),
    (
        "wa download: a shared cache may keep the file",
        WA,
        '                "Cache-Control": "private, no-store",',
        '                "Cache-Control": "public, max-age=3600",',
        ROUTE,
    ),
    (
        "wa download: an expired file looks like one that never existed",
        WA,
        "                status_code=410 if isinstance(exc, ArtifactExpired) else 404,",
        "                status_code=404,",
        ROUTE,
    ),
    (
        "wa download: the filename goes into the header as written",
        WA,
        '        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", artifact.name)',
        "        fallback = artifact.name",
        ROUTE,
    ),
    # ---- the tool: asks first, only where a person is in charge ----------------------------
    (
        "ta tool: publishing needs no approval",
        TA,
        "            category=Category.NET_WRITE,",
        "            category=Category.READ,",
        POLICY,
    ),
    (
        "ta tool: a run woken by outside content may publish",
        TA,
        "            profiles=frozenset({FULL, SCHEDULED}),",
        '            profiles=frozenset({FULL, SCHEDULED, "untrusted"}),',
        POLICY,
    ),
    (
        "ta tool: the nightly memory run may publish",
        TA,
        "            profiles=frozenset({FULL, SCHEDULED}),",
        '            profiles=frozenset({FULL, SCHEDULED, "dream"}),',
        POLICY,
    ),
    (
        "ta tool: the name and caption are not checked before approval",
        TA,
        "            floor=_publication_floor,",
        "            floor=None,",
        POLICY,
    ),
    (
        "ta tool: the policy judges the path as the model wrote it",
        TA,
        "            detail_for=_canonical_path,",
        "            detail_for=None,",
        POLICY,
    ),
    (
        "ta tool: deny rules see only the path as the model wrote it",
        TA,
        "            views_for=_path_views,",
        "            views_for=None,",
        POLICY,
    ),
    (
        "ta tool: offered while publication is turned off",
        TA,
        '                None if app.config.artifacts.enabled else "artifact publication is disabled"',
        "                None",
        POLICY,
    ),
    (
        "ta tool: the notification is not tied to the task's queue",
        TA,
        "    if artifact.outbox_id is not None and artifact.outbox_id not in rc.queued_messages:",
        "    if False:",
        POLICY,
    ),
    (
        "ta tool: a reused snapshot is reported as new",
        TA,
        "    if publication.reused:",
        "    if False:",
        POLICY,
    ),
    (
        "ta tool: an expired snapshot is described as available",
        TA,
        "        if artifact.expires_at <= rc.app.clock.now():",
        "        if False:",
        POLICY,
    ),
    (
        "ta tool: a person waiting is not told apart from a timer",
        TA,
        "            interactive=rc.origin.interactive,",
        "            interactive=False,",
        POLICY,
    ),
    # ---- the outbox: one upload, never two -----------------------------------------------
    (
        "ob must_arrive: a file whose upload outcome is unknown is sent again",
        OB,
        '        if self.kind == "artifact":',
        "        if False:",
        OUTBOX,
    ),
    (
        "ob must_arrive: the database query sends a file again",
        OB,
        "MUST_ARRIVE_SQL = \"\"\"(kind != 'artifact' AND (urgency = 'urgent' OR kind = 'approval_request'",
        "MUST_ARRIVE_SQL = \"\"\"((urgency = 'urgent' OR kind = 'approval_request'",
        OUTBOX,
    ),
    # ---- DingTalk: where the Stream credential may go, and what an unclear send means -----
    (
        "dt stream: a plain ws endpoint",
        DT,
        '        parts.scheme != "wss"',
        '        parts.scheme not in ("ws", "wss")',
        DINGTALK,
    ),
    (
        "dt stream: any host",
        DT,
        STREAM_HOST,
        "        or False",
        DINGTALK,
    ),
    (
        "dt stream: a lookalike host",
        DT,
        STREAM_HOST,
        '        or not host.endswith("dingtalk.com")',
        DINGTALK,
    ),
    (
        "dt stream: another port",
        DT,
        "        or port not in (None, 443)",
        "        or False",
        DINGTALK,
    ),
    (
        "dt stream: a user name in the endpoint",
        DT,
        "        or parts.username is not None",
        "        or False",
        DINGTALK,
    ),
    (
        "dt stream: a fragment on the endpoint",
        DT,
        "        or parts.fragment",
        "        or False",
        DINGTALK,
    ),
    (
        "dt stream: a query already on the endpoint",
        DT,
        "        or parts.query",
        "        or False",
        DINGTALK,
    ),
    (
        "dt stream: a path that is not absolute",
        DT,
        '        or not parts.path.startswith("/")',
        "        or False",
        DINGTALK,
    ),
    (
        "dt stream: the ticket goes into the address as written",
        DT,
        'urlencode({"ticket": ticket})',
        '"ticket=" + ticket',
        DINGTALK,
    ),
    (
        "dt stream: an untrusted endpoint is retried as if temporary",
        DT,
        'raise DingTalkApiError("DingTalk returned an untrusted Stream endpoint", permanent=True)',
        'raise DingTalkApiError("DingTalk returned an untrusted Stream endpoint")',
        DINGTALK,
    ),
    (
        "dt http: a redirect is followed with the token attached",
        DT,
        "                follow_redirects=False,",
        "                follow_redirects=True,",
        DINGTALK,
    ),
    (
        "dt http: a redirect or timeout on a send is a plain failure",
        DT,
        SEND_RESULT_CHECK,
        "        if False:",
        DINGTALK,
    ),
    (
        "dt http: a 5xx on a send is a plain failure",
        DT,
        "        if status >= 500:\n            if sending:",
        "        if status >= 500:\n            if False:",
        DINGTALK,
    ),
    (
        "dt http: an interrupted send is a plain failure",
        DT,
        "            if sending:\n                raise DeliveryUncertain(detail) from None",
        "            if False:\n                raise DeliveryUncertain(detail) from None",
        DINGTALK,
    ),
    (
        "dt http: an error code from the platform is quoted back",
        DT,
        '    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value) else ""',
        "    return value",
        DINGTALK,
    ),
    (
        "dt http: Retry-After is believed whatever it says",
        DT,
        "    return max(1.0, min(value, 3600.0)) if math.isfinite(value) else 60.0",
        "    return value",
        DINGTALK,
    ),
    (
        "dt http: a refusal is retried as if temporary",
        DT,
        "                permanent=status in (400, 401, 403, 404),",
        "                permanent=False,",
        DINGTALK,
    ),
    (
        "dt token: a token is kept after it has expired",
        DT,
        "            if self._token and self._now() < self._token_until:",
        "            if self._token:",
        DINGTALK,
    ),
    (
        "dt token: the full lifetime is trusted",
        DT,
        "                self._now() + max(0.0, expiry - min(60.0, expiry / 10)),",
        "                self._now() + expiry,",
        DINGTALK,
    ),
    (
        "dt token: a boolean lifetime is accepted",
        DT,
        "                or isinstance(expiry, bool)",
        "                or False",
        DINGTALK,
    ),
    (
        "dt token: an infinite lifetime is accepted",
        DT,
        "                or not math.isfinite(expiry)",
        "                or False",
        DINGTALK,
    ),
    (
        "dt token: a lifetime of zero is accepted",
        DT,
        "                or expiry <= 0",
        "                or False",
        DINGTALK,
    ),
    # ---- the preview: a half-written credential is not shown ----------------------------
    (
        "lv preview: a half-written credential is shown",
        LV,
        "    if not settled:",
        "    if False:",
        STREAM,
    ),
    (
        "lv preview: all but one character of a credential is shown",
        LV,
        "            for size in range(min(len(secret) - 1, len(text)), 0, -1):",
        "            for size in range(min(len(secret) - 2, len(text)), 0, -1):",
        STREAM,
    ),
    (
        "lv preview: hidden characters are not removed before the comparison",
        LV,
        "    text = scrub_surrogates(strip_invisible(text))",
        "    text = text",
        STREAM,
    ),
    (
        "lv preview: credentials are compared without the same clean-up",
        LV,
        "    known = [scrub_surrogates(strip_invisible(value)) for value in known_secret_values()]",
        "    known = list(known_secret_values())",
        STREAM,
    ),
    (
        "lv preview: a whole credential is not redacted",
        LV,
        "    return redact(text[:end], known)",
        "    return text[:end]",
        STREAM,
    ),
    (
        "lv draft: a preview has no size limit",
        LV,
        "        raw = raw[:MAX_DRAFT_CHARS]",
        "        raw = raw",
        STREAM,
    ),
    (
        "lv draft: a cut-off preview is treated as finished",
        LV,
        '            "text": preview_text(raw, settled=complete and not truncated),',
        '            "text": preview_text(raw, settled=complete),',
        STREAM,
    ),
    (
        "lv draft: any number of runs may be previewed",
        LV,
        "        if run_id not in self._drafts and len(self._drafts) >= MAX_DRAFTS:",
        "        if False:",
        STREAM,
    ),
    (
        "lv draft: a finished run keeps its preview",
        LV,
        "        if self._drafts.pop(run_id, None) is not None:",
        "        if False:",
        STREAM,
    ),
    (
        "lv draft: text for a run that never started makes a preview",
        LV,
        "        if run_id not in self._drafts:\n            return\n        previous = self._draft_text[run_id]",
        '        previous = self._draft_text.get(run_id, "")',
        STREAM,
    ),
    (
        "lv draft: a new model attempt continues the old attempt's text",
        LV,
        '        self._draft_text[run_id] = ""',
        '        self._draft_text.setdefault(run_id, "")',
        STREAM,
    ),
    # ---- the stream: only answer text, cleaned up completely -----------------------------
    (
        "st stream: tool arguments and reasoning reach the page",
        ST,
        '            elif data.type == "response.output_text.delta":',
        '            elif data.type.endswith(".delta"):',
        STREAM,
    ),
    (
        "st stream: the preview outlives the run",
        ST,
        "        live.response_finished(run_id)",
        "        pass",
        STREAM,
    ),
    (
        "st stream: a failure inside the stream is swallowed",
        ST,
        "        if result.run_loop_exception is not None:\n            raise result.run_loop_exception",
        "        if False:\n            raise result.run_loop_exception",
        STREAM,
    ),
    (
        "st stream: cleanup tasks are not awaited after a cancel",
        ST,
        "            async for _ in result.stream_events():\n                pass",
        "            pass",
        STREAM,
    ),
    (
        "st stream: a failure during cleanup hides the first failure",
        ST,
        "            raise failure from None",
        "            raise",
        STREAM,
    ),
    # ---- provider endpoints: which address gets which key, and what is trusted ------------
    (
        "ep native: plain http is the native API",
        EP,
        '            url.scheme == "https"',
        '            url.scheme in ("http", "https")',
        PROVIDER,
    ),
    (
        "ep native: a lookalike host is the native API",
        EP,
        '            and url.hostname == "api.openai.com"',
        '            and (url.hostname or "").endswith("openai.com")',
        PROVIDER,
    ),
    (
        "ep native: another port is the native API",
        EP,
        "            and url.port in (None, 443)",
        "            and True",
        PROVIDER,
    ),
    (
        "ep native: another path is the native API",
        EP,
        '            and url.path.rstrip("/") == "/v1"',
        '            and url.path.startswith("/v1")',
        PROVIDER,
    ),
    (
        "ep native: an address with a user name is the native API",
        EP,
        "            and url.username is None",
        "            and True",
        PROVIDER,
    ),
    (
        "ep native: an address with a query is the native API",
        EP,
        "            and not url.query",
        "            and True",
        PROVIDER,
    ),
    (
        "ep native: an address with a fragment is the native API",
        EP,
        "            and not url.fragment",
        "            and True",
        PROVIDER,
    ),
    (
        "ep native: an address that cannot be read is the native API",
        EP,
        "    except ValueError:\n        return False",
        "    except ValueError:\n        return True",
        PROVIDER,
    ),
    (
        "ep key: a named custom credential may be missing",
        EP,
        "            and self.api_key_env == DEFAULT_KEY_ENV",
        "            and True",
        PROVIDER,
    ),
    (
        "ep key: the native API may run without a key",
        EP,
        "            and not is_native_openai(self.base_url)",
        "            and True",
        PROVIDER,
    ),
    (
        "ep key: an empty variable counts as a key",
        EP,
        "        return os.environ.get(self.api_key_env) or None",
        "        return os.environ.get(self.api_key_env)",
        PROVIDER,
    ),
    (
        "ep model: the base address from the environment is ignored",
        EP,
        '    return Endpoint(cfg.base_url or os.environ.get("OPENAI_BASE_URL") or None, cfg.api_key_env)',
        "    return Endpoint(cfg.base_url or None, cfg.api_key_env)",
        PROVIDER,
    ),
    (
        "ep speech: its own credential name is ignored",
        EP,
        "        media.transcribe_api_key_env or inherited.api_key_env,",
        "        inherited.api_key_env,",
        PROVIDER,
    ),
    (
        "ep speech: its own address is ignored",
        EP,
        "        media.transcribe_base_url or inherited.base_url,",
        "        inherited.base_url,",
        PROVIDER,
    ),
    (
        "ep search: a chat-completions model is offered hosted search",
        EP,
        '    if cfg.api != "responses":\n        return False',
        "    if False:\n        return False",
        PROVIDER,
    ),
    (
        "ep search: a declared no is overridden by the address",
        EP,
        "    return declared if declared is not None else is_native_openai(model_endpoint(cfg).base_url)",
        "    return is_native_openai(model_endpoint(cfg).base_url)",
        PROVIDER,
    ),
    # ---- the service file: no provider key is copied into it -----------------------------
    (
        "sv env: a provider credential is copied into the job file",
        SV,
        "            if name in {cfg.model.api_key_env, cfg.media.transcribe_api_key_env}:",
        "            if False:",
        SERVICE,
    ),
    (
        "sv env: the speech credential is copied into the job file",
        SV,
        "            if name in {cfg.model.api_key_env, cfg.media.transcribe_api_key_env}:",
        "            if name in {cfg.model.api_key_env}:",
        SERVICE,
    ),
    (
        "sv env: the model credential is copied into the job file",
        SV,
        "            if name in {cfg.model.api_key_env, cfg.media.transcribe_api_key_env}:",
        "            if name in {cfg.media.transcribe_api_key_env}:",
        SERVICE,
    ),
    (
        "sv hint: a missing model key is not mentioned",
        SV,
        "    if model_endpoint(cfg.model).missing_key:",
        "    if False:",
        SERVICE,
    ),
]
