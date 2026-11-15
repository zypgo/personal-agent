"""User configuration (`config.toml`) and environment loading.

Secrets never live in `config.toml`; they come from the environment / `.env`.
Unknown keys are rejected so typos are noticed instead of silently ignored.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from personal_agent.clock import parse_quiet_hours
from personal_agent.paths import Paths

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")


def _check_env_name(value: str, what: str) -> str:
    """The NAME of an environment variable (capital letters, digits, underscores), never a value.

    Someone who pastes a secret here by mistake must not get it printed back by an error message,
    a `pa doctor` line or a log, all of which name the variable. A random secret is almost never
    made only of capital letters, digits and underscores, so asking for that shape stops it."""
    if not _ENV_NAME.fullmatch(value):
        raise ValueError(
            f"{what} is the NAME of an environment variable, in capital letters (like "
            "GITHUB_WEBHOOK_SECRET), not the secret itself"
        )
    return value


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentConfig(_Section):
    name: str = "Assistant"
    owner_name: str | None = None
    timezone: str | None = None
    """IANA zone name. `None` = detect from the system."""
    max_turns: int = Field(25, ge=1, le=200)
    """Model calls allowed in one run before the harness stops it."""
    history_items: int = Field(400, ge=20)
    """How many of the newest session items the model sees. The log keeps everything; this is a
    stopgap until summarising compaction lands."""


class ModelConfig(_Section):
    main: str = "gpt-6.1-sol"
    """Conversations and background work."""
    fast: str = "gpt-6-luna"
    """Cheap side jobs (summaries, guardian review, titles)."""
    reasoning_effort: ReasoningEffort = "medium"
    reasoning_context: Literal["auto", "current_turn", "all_turns"] | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    base_url: str | None = None
    """OpenAI-compatible endpoint. `None` = the OpenAI default / `OPENAI_BASE_URL`."""
    api: Literal["responses", "chat_completions"] = "responses"
    """`chat_completions` for OpenAI-compatible servers that do not implement the Responses API."""
    store: bool = False
    """False: OpenAI does not keep responses; the full history lives in our own state.db."""
    reasoning_item_ids: Literal["preserve", "omit"] = "preserve"
    """`omit` strips reasoning item IDs when history is replayed to the API. Use it only as a
    fallback if `pa doctor --live` reports that the API rejects replayed reasoning items."""
    prompt_cache_retention: Literal["in_memory", "24h"] | None = None
    """How long OpenAI may keep the processed static start of the prompt (instructions and tool
    definitions, thousands of tokens) so the next run pays a tenth for it. `None`: OpenAI's own
    default, minutes. `"24h"` keeps it for a day, which suits an agent that wakes up now and then,
    at the price of OpenAI holding data derived from the prompt that long."""
    timeout_s: float = Field(180.0, gt=0)
    """Per model call attempt."""
    max_retries: int = Field(3, ge=0, le=10)


class MemoryConfig(_Section):
    max_file_bytes: int = Field(65_536, ge=1_024)
    max_total_bytes: int = Field(4_194_304, ge=65_536)
    max_files: int = Field(500, ge=10)
    view_max_chars: int = Field(16_000, ge=1_000)
    index_max_lines: int = Field(200, ge=10)
    """Only the first N lines of state/MEMORY.md are injected into every run."""
    index_max_bytes: int = Field(25_000, ge=1_000)
    recent_messages: int = Field(5, ge=0, le=20)
    """How many of the messages you last sent the owner a background run is shown when it starts
    a new conversation, so that it does not tell them the same thing twice. 0 shows none."""
    recent_message_chars: int = Field(400, ge=60, le=2_000)
    """Each of those messages is cut to this many characters."""
    task_index_lines: int = Field(20, ge=0, le=100)
    """How many unfinished task files (state/tasks/) are listed at the start of every run. 0 lists
    none; the files stay where they are and the agent can still open them."""


class ContextConfig(_Section):
    """Keeping a long conversation inside the model's reach (see harness/compaction.py)."""

    compact: bool = True
    """Summarise the old part of a long conversation instead of letting it slide out of view. The
    log keeps every word either way; this only changes what the model is shown."""
    compact_at_tokens: int = Field(40_000, ge=2_000)
    """The conversation is summarised once what the model would be shown is estimated above this."""
    keep_recent_tokens: int = Field(10_000, ge=500)
    """About this much of the newest part is kept word for word (it starts at a message of yours)."""
    model: str | None = None
    """Writes the summary. `None` uses `model.fast`."""
    summary_max_tokens: int = Field(3_000, ge=300, le=16_000)
    """What the summary may use, including the model's own thinking."""
    input_max_chars: int = Field(120_000, ge=10_000)
    """The most of the old conversation shown to the model that writes the summary."""
    result_chars: int = Field(600, ge=100, le=5_000)
    """Each tool result is cut to this many characters for the summary writer (the log keeps all)."""
    timeout_s: float = Field(120.0, gt=0)
    retry_after_s: float = Field(3_600.0, ge=0)
    """After a failed attempt on a conversation, wait this long before trying it again."""


class DreamingConfig(_Section):
    """The nightly pass over what happened (see harness/dreaming.py)."""

    enabled: bool = True
    at: str = "03:30"
    """Local time of day (HH:MM) after which the pass runs, once a day. A computer that was asleep
    runs it when it wakes."""
    min_new_items: int = Field(20, ge=1)
    """A night with less new conversation than this (items in the session logs) is skipped."""
    max_digest_chars: int = Field(60_000, ge=5_000, le=400_000)
    """How much of the new conversation one pass reads; what does not fit waits for the next."""
    max_cost_usd: float = Field(0.30, gt=0)
    """One pass above this is stopped (never more than `budget.per_run_usd`)."""
    max_deleted_files: int = Field(10, ge=1)
    max_deleted_share: float = Field(0.5, gt=0, le=1)
    """A pass that deletes more notes than this, or this share of all of them, is undone entirely."""

    @field_validator("at")
    @classmethod
    def _time_of_day(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("a time of day written HH:MM, such as 03:30")
        return value


class RuntimeConfig(_Section):
    concurrency: int = Field(2, ge=1, le=16)
    """Sessions worked on at the same time (one session is only ever handled by one run)."""
    run_timeout_s: float = Field(900.0, gt=0)
    """A single run that takes longer than this is stopped."""
    poll_s: float = Field(1.0, ge=0.05)
    """How often an idle worker checks the inbox for events added by other processes."""
    shutdown_grace_s: float = Field(30.0, ge=0)
    """On Ctrl-C or SIGTERM, runs in flight get this long to finish; the rest are put back untouched."""
    tool_timeout_s: float = Field(120.0, gt=0)
    """A single tool call that takes longer than this is abandoned (its result says so)."""
    tool_output_chars: int = Field(20_000, ge=1_000)
    """Longer tool output is cut; the model is told how much was left out."""


class PolicyConfig(_Section):
    mode: Literal["read_only", "supervised", "auto", "autonomous"] = "supervised"
    """read_only: look but never act. supervised: asks before anything that changes the world.
    auto: a small reviewer model decides, escalating to you when unsure. autonomous: no
    questions (only your deny rules and always_ask still apply)."""
    always_ask: list[str] = []
    """Patterns like "shell:*" or "mcp_*". These ask even in auto and autonomous mode."""
    allow: list[str] = []
    """Patterns that are pre-approved, for example "shell:git status*"."""
    deny: list[str] = []
    """Patterns that are never allowed in any mode."""
    allow_when_tainted: list[str] = []
    """Allow patterns that stay allowed after the run has read untrusted content."""
    approval_timeout_hours: float = Field(12.0, gt=0)
    """An approval request nobody answers within this time counts as a refusal."""
    i_understand_autonomous_risk: bool = False

    @model_validator(mode="after")
    def _autonomous_must_be_acknowledged(self) -> PolicyConfig:
        if self.mode == "autonomous" and not self.i_understand_autonomous_risk:
            raise ValueError(
                'policy.mode = "autonomous" lets the agent act without asking; '
                "set policy.i_understand_autonomous_risk = true to confirm"
            )
        return self


class BudgetConfig(_Section):
    per_run_usd: float = Field(1.0, gt=0)
    """A single run that spends more than this is stopped and reported (usually a loop)."""
    daily_usd: float = Field(5.0, gt=0)
    """Across all runs since local midnight. Reaching it pauses work until tomorrow; nothing is lost."""


class NotifyConfig(_Section):
    channel: str = "auto"
    """Where messages go when no conversation is waiting (scheduled work, alerts): `auto`, or a
    channel by name (`cli`, `telegram`, `web`, a plugin's).

    `auto` picks the chat app that can reach you on a phone and that you last wrote from; if you
    never used one, the web page when it is on (it shows the message when you next open it, it
    cannot ping you), else the terminal."""
    quiet_hours: str | None = None
    """Local time window such as "23:00-08:00". Only urgent messages are delivered inside it."""
    max_per_hour: int = Field(6, ge=1)
    """Non-urgent messages per channel per hour; the rest wait for the next hour."""

    @field_validator("quiet_hours")
    @classmethod
    def _quiet_hours_are_valid(cls, value: str | None) -> str | None:
        if value is not None:
            parse_quiet_hours(value)
        return value


class SchedulerConfig(_Section):
    misfire_grace_s: int = Field(3600, ge=0)
    """A recurring schedule that is overdue by more than this (the computer was asleep) is
    treated according to its misfire policy instead of firing as if on time."""
    min_interval_s: int = Field(300, ge=60)
    """Shortest allowed gap between two firings of a recurring schedule."""
    max_timers: int = Field(50, ge=1)


class SandboxConfig(_Section):
    backend: Literal["auto", "seatbelt", "docker", "none"] = "auto"
    """Where shell commands run. `auto` picks seatbelt on macOS, else docker. If neither works the
    shell tool is simply not offered (we never quietly run commands unprotected)."""
    i_understand_no_sandbox: bool = False
    """Required for `backend = "none"`: commands then run as you, with all your files and network."""
    image: str = "python:3.13-slim"
    """Docker image for the docker backend. We never pull it for you: `docker pull` it yourself."""
    memory_mb: int = Field(512, ge=64)
    default_timeout_s: float = Field(60.0, gt=0)
    max_timeout_s: float = Field(600.0, gt=0)
    extra_read: list[str] = []
    """Extra directories commands may read (for example a toolchain in your home directory)."""
    extra_write: list[str] = []
    """Extra directories commands may write. Think twice before adding anything here."""


class NetConfig(_Section):
    allow_private: list[str] = []
    """Hostnames, IPs or CIDRs that may be reached even though they are on your own network
    (for example "homeassistant.local" or "192.168.1.0/24"). Everything private is blocked by
    default; cloud metadata addresses stay blocked even if listed here."""
    timeout_s: float = Field(20.0, gt=0)
    max_response_bytes: int = Field(2_000_000, ge=10_000)
    max_redirects: int = Field(5, ge=0, le=10)
    user_agent: str = "personal-agent/0.1"

    @field_validator("allow_private")
    @classmethod
    def _entries_are_hosts_ips_or_networks(cls, value: list[str]) -> list[str]:
        for entry in value:
            text = entry.strip()
            try:
                if "/" in text:
                    ipaddress.ip_network(text, strict=False)
                elif not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?", text):
                    ipaddress.ip_address(text.split("%", 1)[0])  # an IPv6 literal
            except ValueError as exc:
                raise ValueError(
                    f"{entry!r} is not a host name, an IP address or a network like 192.168.1.0/24"
                ) from exc
        return value


class SecretConfig(_Section):
    env: str
    """Name of the environment variable (or `.env` entry) that holds the value."""
    hosts: list[str] = Field(min_length=1)
    """Hosts the value may be sent to: an exact name or `*.example.com`. The model writes
    `{{secret:NAME}}` and never sees the value; a request to any other host is refused."""

    @field_validator("env")
    @classmethod
    def _env_is_a_name(cls, value: str) -> str:
        return _check_env_name(value, "env")


class ToolsConfig(_Section):
    files: bool = True
    """list_files, read_file, write_file, edit_file inside the workspace."""
    file_max_bytes: int = Field(1_000_000, ge=1_000)
    """Largest file the agent may write; reading pages through bigger files line by line."""
    shell: bool = True
    """Offered only when a sandbox works (see [sandbox])."""
    web_fetch: bool = True
    http_request: bool = True
    web_search: Literal["auto", "cached", "off"] = "auto"
    """OpenAI's hosted search, offered only on the OpenAI Responses API. `cached` searches OpenAI's
    index without opening live pages, so a page cannot be made to receive anything from you."""
    web_search_call_usd: float = Field(0.01, ge=0)
    """Estimated cost of one hosted search, added to the run's spend (the API bills it separately)."""


class SkillsConfig(_Section):
    enabled: bool = True
    """Offer the skills installed in PA_HOME/skills (see `pa skills`)."""
    description_chars: int = Field(300, ge=60, le=1024)
    """Each skill's description is shortened to this in the list the model always sees."""
    catalog_chars: int = Field(8_000, ge=500)
    """Size of that list. Skills beyond it are shown by name only; the model can still load them."""
    max_body_chars: int = Field(40_000, ge=2_000)
    """A skill whose instructions are longer is cut when it is loaded (the model is told)."""


class MediaConfig(_Section):
    images: bool = True
    """Accept pictures from the owner and show them to the model."""
    max_image_bytes: int = Field(8_000_000, ge=100_000, le=20_000_000)
    images_in_context: int = Field(2, ge=0, le=10)
    """Each request carries only the newest N pictures; older ones become a one-line note, so a
    long conversation does not keep paying for (and re-uploading) every picture."""
    voice: bool = True
    """Turn voice messages into text with OpenAI speech-to-text before the agent sees them."""
    transcribe_model: str = "gpt-transcribe"
    """`whisper-1` and the `gpt-4o-*-transcribe` models are scheduled to shut down on 2027-02-26."""
    language: str | None = None
    """ISO-639-1 hint such as "zh" or "en" (improves accuracy for short clips). `None` = detect."""
    max_voice_seconds: int = Field(300, ge=5, le=3600)
    max_voice_bytes: int = Field(20_000_000, ge=10_000, le=25_000_000)
    """The speech API accepts files up to 25 MB."""


class TelegramConfig(_Section):
    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"  # noqa: S105 - the NAME of the variable
    """Name of the `.env` entry holding the token from @BotFather. The token is never in config.toml."""
    api_base: str = "https://api.telegram.org"
    """Change only to reach Telegram through your own Bot API server or reverse proxy."""
    proxy: str | None = None
    """For example "socks5://127.0.0.1:1080" or "http://127.0.0.1:7890" when Telegram is not
    reachable directly. By default the usual HTTPS_PROXY / ALL_PROXY variables are honoured."""
    poll_timeout_s: int = Field(50, ge=1, le=60)
    """Long-poll length. Telegram answers as soon as a message arrives."""
    typing: bool = True
    """Show "typing..." while the agent works on a message."""

    @field_validator("token_env")
    @classmethod
    def _token_env_is_a_name(cls, value: str) -> str:
        return _check_env_name(value, "token_env")

    @field_validator("api_base")
    @classmethod
    def _api_base_is_https(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError("api_base must be an http:// or https:// address")
        if parts.username or parts.password:
            raise ValueError("do not put credentials in api_base")
        if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
            raise ValueError("plain http is only allowed for this machine (localhost)")
        return value.rstrip("/")


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def _check_listen_host(value: str) -> str:
    """An IP address (127.0.0.1, 0.0.0.0, ::1) or a plain name such as localhost: no scheme, no
    port, no path. Anything else would only fail later, when the listener starts."""
    host = value.strip()
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        pass
    if _HOSTNAME.fullmatch(host):
        return host
    raise ValueError("host is an address such as 127.0.0.1 or 0.0.0.0, without http:// or a port")


def _check_public_url(value: str | None) -> str | None:
    """Where the outside world reaches a listener: an http(s) origin and nothing else."""
    if value is None or not value.strip():
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("public_url must look like https://my-mac.tailnet-name.ts.net")
    if parts.username or parts.password:
        raise ValueError("do not put credentials in public_url")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            "public_url is an address without a path (https://host or https://host:port)"
        )
    return f"{parts.scheme}://{parts.netloc}".lower()


class WebConfig(_Section):
    """The chat page you open in a browser (your phone's, too)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    """Where to listen. 127.0.0.1 is this computer only. To use the page from a phone, keep this
    and put Tailscale Serve (or another HTTPS proxy you trust) in front: `tailscale serve --bg 8765`."""
    port: int = Field(8765, ge=1, le=65535)
    public_url: str | None = None
    """The address the phone uses, e.g. "https://my-mac.tailnet-name.ts.net". Pairing links use it,
    requests from that address are accepted as our own, and an https one makes the session cookie
    Secure."""
    allowed_hosts: list[str] = []
    """More names this page may be reached under (the public_url's name and localhost always may)."""
    session_days: int = Field(30, ge=1, le=365)
    """A paired browser stays signed in this long after it was last used."""
    invite_minutes: int = Field(10, ge=1, le=1440)
    """How long a pairing link from `pa web pair` works. It works once."""

    @field_validator("host")
    @classmethod
    def _host_is_an_address(cls, value: str) -> str:
        return _check_listen_host(value)

    @field_validator("public_url")
    @classmethod
    def _public_url_is_an_origin(cls, value: str | None) -> str | None:
        return _check_public_url(value)

    @field_validator("allowed_hosts")
    @classmethod
    def _allowed_hosts_are_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not (_HOSTNAME.fullmatch(name) or name.lower() in _LOOPBACK_HOSTS):
                raise ValueError(f"{name!r} is not a host name (no scheme, no port)")
        return [name.lower() for name in value]


WEBHOOK_SCHEMES = ("github", "gitlab", "standard", "generic")
_ROUTE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class WebhookRoute(_Section):
    """One thing that may call the agent: `POST /hook/NAME` with a signature only you and the
    sender can make."""

    scheme: Literal["github", "gitlab", "standard", "generic"]
    """How the sender proves it is them. Fixed per route, never guessed from the request:
    github (X-Hub-Signature-256), gitlab (X-Gitlab-Token), standard (the Standard Webhooks /
    Svix headers) or generic (X-Webhook-Timestamp + X-Webhook-Signature, see the README)."""
    secret_env: str
    """Name of the `.env` entry holding the shared secret. Never the secret itself."""
    prompt: str = Field("", max_length=4000)
    """What to do with this kind of event, in your words. The agent follows it; it never follows
    anything inside the event itself."""
    events: list[str] = []
    """Only these event types wake the agent (github: push, pull_request...). Empty: all."""
    tolerance_s: int = Field(300, ge=30, le=3600)
    """How old a signed timestamp may be (the schemes that carry one)."""

    @field_validator("secret_env")
    @classmethod
    def _secret_env_is_a_name(cls, value: str) -> str:
        return _check_env_name(value, "secret_env")


class WebhooksConfig(_Section):
    """A second listener, only for webhooks. Separate from [web] on purpose: this is the one you
    may have to put on the internet (a tunnel), and the chat page should not be reachable there."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(8766, ge=1, le=65535)
    public_url: str | None = None
    """Where senders reach this listener, shown by `pa webhooks` so you can copy the address."""
    max_body_bytes: int = Field(262_144, ge=1_024, le=5_000_000)
    rate_per_minute: int = Field(30, ge=1, le=600)
    """Accepted deliveries per route per minute; more get 429."""
    routes: dict[str, WebhookRoute] = {}

    @field_validator("host")
    @classmethod
    def _host_is_an_address(cls, value: str) -> str:
        return _check_listen_host(value)

    @field_validator("public_url")
    @classmethod
    def _public_url_is_an_origin(cls, value: str | None) -> str | None:
        return _check_public_url(value)

    @field_validator("routes")
    @classmethod
    def _route_names_are_simple(cls, value: dict[str, WebhookRoute]) -> dict[str, WebhookRoute]:
        for name in value:
            if not _ROUTE_NAME.fullmatch(name):
                raise ValueError(
                    f"route name {name!r} must be lowercase letters, digits, - or _ (it is part of the URL)"
                )
        return value


ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""`${NAME}` in an MCP server's `env` or `headers`: filled in from the agent's environment (`.env`)
when the server starts, so a credential never has to be written into config.toml."""


def env_references(text: str) -> list[str]:
    return ENV_REFERENCE.findall(text)


class McpServerConfig(_Section):
    """One Model Context Protocol server: a program or web service that offers tools."""

    name: str
    """Short name, used in tool names (`mcp__<name>__<tool>`) and in `pa mcp`."""
    enabled: bool = True
    transport: Literal["stdio", "http", "sse"] = "stdio"
    """`stdio`: a program the agent starts. `http`: a Streamable HTTP endpoint. `sse`: the older
    server-sent-events endpoint."""

    # stdio
    command: str | None = None
    args: list[str] = []
    """Arguments of the program. `${NAME}` is replaced by a value from `.env`."""
    env: dict[str, str] = {}
    """Extra environment for the program. It gets nothing else from the agent's environment except
    HOME, PATH, USER, LOGNAME, SHELL and TERM. Use `${NAME}` to pass a value from `.env`."""
    cwd: str | None = None

    # http / sse
    url: str | None = None
    """The endpoint. `${NAME}` is replaced by a value from `.env` (for keys that go in the query)."""
    headers: dict[str, str] = {}
    """Request headers. Use `${NAME}` for credentials (`Authorization = "Bearer ${TOKEN}"`)."""

    # what the agent may do with the tools
    read_only: list[str] = []
    """Tool-name patterns (`get_*`, `list_*`) you know only look at things. They count as reads;
    every other tool of this server asks for your approval like any action that changes things."""
    trust_annotations: bool = False
    """Believe the server's own `readOnlyHint` (a tool marked read-only counts as a read). Off by
    default: a server describes itself, and a bad one can lie."""
    allow_tools: list[str] = []
    """If set, only tools matching one of these patterns are offered."""
    deny_tools: list[str] = []
    profiles: list[Literal["full", "scheduled", "untrusted"]] = ["full", "scheduled"]
    """Which kinds of runs get these tools. `untrusted` (runs woken by a webhook or outside mail)
    is allowed only if you list it."""
    taints: bool = True
    """Results are outside content: fenced as data, and a run that read one asks before acting.
    Turn off only for a server that returns nothing but your own data."""
    pin: Literal["ask", "auto"] = "ask"
    """`ask`: no tool is offered until you have read its definition (`pa mcp trust NAME`).
    `auto`: the first definitions are accepted as they are. In both modes a tool that is new or
    whose description changed later is withheld until you review it."""
    connect_timeout_s: float = Field(30.0, gt=0, le=300)
    call_timeout_s: float = Field(60.0, gt=0, le=1800)

    @field_validator("name")
    @classmethod
    def _name_is_short_and_plain(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,23}", value):
            raise ValueError(
                f"server name {value!r} must be letters, digits and hyphens (at most 24), "
                "starting with a letter"
            )
        return value

    @field_validator("env")
    @classmethod
    def _env_names_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"{key!r} is not an environment variable name")
        return value

    @field_validator("headers")
    @classmethod
    def _header_names_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", key):
                raise ValueError(f"{key!r} is not an HTTP header name")
        return value

    @field_validator("read_only", "allow_tools", "deny_tools")
    @classmethod
    def _patterns_are_not_empty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("a tool-name pattern cannot be empty")
        return value

    @field_validator("profiles")
    @classmethod
    def _at_least_one_profile(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("list at least one kind of run, or set enabled = false")
        return value

    @model_validator(mode="after")
    def _fits_its_transport(self) -> McpServerConfig:
        from personal_agent.security.redact import find_secrets

        if self.transport == "stdio":
            if not (self.command or "").strip():
                raise ValueError(f"server {self.name!r}: a stdio server needs a `command`")
            if self.url or self.headers:
                raise ValueError(f"server {self.name!r}: `url` and `headers` are for http servers")
        else:
            if not (self.url or "").strip():
                raise ValueError(f"server {self.name!r}: an http server needs a `url`")
            if self.command or self.args or self.env or self.cwd:
                raise ValueError(
                    f"server {self.name!r}: `command`, `args`, `env` and `cwd` are for stdio servers"
                )
            self._check_url(ENV_REFERENCE.sub("x", self.url or ""))

        def literal_secret(text: str) -> bool:
            return bool(find_secrets(ENV_REFERENCE.sub("", text)))

        for where, values in (("env", self.env), ("headers", self.headers)):
            for key, value in values.items():
                if literal_secret(value) or find_secrets(f"{key}={value}"):
                    raise ValueError(
                        f"server {self.name!r}: {where}.{key} looks like a real credential. "
                        "Put it in .env and write ${NAME} here instead."
                    )
        places = [(f"args[{i}]", arg) for i, arg in enumerate(self.args)] + [
            ("url", self.url or "")
        ]
        for where, text in places:
            if literal_secret(text):
                raise ValueError(
                    f"server {self.name!r}: {where} looks like a real credential (it would be "
                    "stored in config.toml and shown in process lists). "
                    "Put it in .env and write ${NAME} here instead."
                )
        return self

    def _check_url(self, url: str) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError(f"server {self.name!r}: the url must be http:// or https://")
        if parts.username or parts.password:
            raise ValueError(
                f"server {self.name!r}: do not put credentials in the url; use `headers` with ${{NAME}}"
            )
        if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"server {self.name!r}: plain http is only allowed for this machine "
                "(localhost); use https for anything else"
            )

    def referenced_env(self) -> list[str]:
        """Names of the environment variables this server's settings refer to."""
        found: list[str] = []
        for value in (*self.env.values(), *self.headers.values(), *self.args, self.url or ""):
            for name in env_references(value):
                if name not in found:
                    found.append(name)
        return found


class McpConfig(_Section):
    enabled: bool = True
    """Master switch: false ignores every [[mcp.servers]] entry."""
    servers: list[McpServerConfig] = []
    refresh_s: float = Field(300.0, ge=5)
    """How long a server's tool list is trusted before it is asked again."""
    retry_s: float = Field(30.0, ge=1)
    """After a server fails, wait this long before trying again (doubling, up to ten minutes)."""
    max_tools: int = Field(80, ge=1, le=120)
    """Most MCP tools offered to the model at once, in all servers together; the model gets
    worse at choosing as the list grows."""

    @field_validator("servers")
    @classmethod
    def _names_are_unique(cls, value: list[McpServerConfig]) -> list[McpServerConfig]:
        seen: set[str] = set()
        for server in value:
            key = server.name.lower()
            if key in seen:
                raise ValueError(f"two MCP servers are called {server.name!r}")
            seen.add(key)
        return value


class PluginsConfig(_Section):
    enabled: list[str] = []
    """Installed Python packages that may add tools (entry point group `personal_agent.plugins`).
    A plugin runs inside the agent with full access to this machine: list only code you trust."""
    settings: dict[str, dict[str, Any]] = {}
    """Settings a plugin reads for itself: `[plugins.settings.NAME]`. No secrets here."""


class Config(_Section):
    agent: AgentConfig = AgentConfig()
    model: ModelConfig = ModelConfig()
    memory: MemoryConfig = MemoryConfig()
    context: ContextConfig = ContextConfig()
    dreaming: DreamingConfig = DreamingConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    policy: PolicyConfig = PolicyConfig()
    budget: BudgetConfig = BudgetConfig()
    notify: NotifyConfig = NotifyConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    sandbox: SandboxConfig = SandboxConfig()
    net: NetConfig = NetConfig()
    tools: ToolsConfig = ToolsConfig()
    skills: SkillsConfig = SkillsConfig()
    mcp: McpConfig = McpConfig()
    plugins: PluginsConfig = PluginsConfig()
    media: MediaConfig = MediaConfig()
    telegram: TelegramConfig = TelegramConfig()
    web: WebConfig = WebConfig()
    webhooks: WebhooksConfig = WebhooksConfig()
    secrets: dict[str, SecretConfig] = {}

    @field_validator("secrets")
    @classmethod
    def _secret_names_are_simple(cls, value: dict[str, SecretConfig]) -> dict[str, SecretConfig]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,40}", name):
                raise ValueError(
                    f"secret name {name!r} must be letters, digits and underscores, "
                    "starting with a letter"
                )
        return value


DEFAULT_CONFIG_TOML = """\
# personal-agent configuration. Secrets do NOT go here: put OPENAI_API_KEY etc. in .env.

[agent]
name = "Assistant"
# owner_name = "Your Name"
# timezone = "Asia/Shanghai"     # IANA name; default: detected from this machine
max_turns = 25                   # model calls per run before the harness stops it
history_items = 400              # newest conversation items the model sees (the log keeps all)

[model]
main = "gpt-6.1-sol"             # conversations and background work
fast = "gpt-6-luna"              # cheap side jobs (summaries, guardian review)
reasoning_effort = "medium"      # none | minimal | low | medium | high | xhigh | max
# reasoning_context = "all_turns"  # auto | current_turn | all_turns
# base_url = "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
# api = "chat_completions"       # for compatible servers without the Responses API
store = false                    # false: OpenAI keeps no copy; history lives in state.db
# reasoning_item_ids = "omit"    # fallback if `pa doctor --live` says replayed reasoning is rejected
# prompt_cache_retention = "24h" # keep the prompt's static start cached for a day: much cheaper runs after
#                                # pauses, but OpenAI then holds data derived from the prompt that long
timeout_s = 180
max_retries = 3

[memory]
max_file_bytes = 65536
max_total_bytes = 4194304
max_files = 500
view_max_chars = 16000
index_max_lines = 200            # lines of memory/state/MEMORY.md injected into every run
index_max_bytes = 25000
recent_messages = 5              # your last messages to the owner, shown to a background run so it does not repeat itself
recent_message_chars = 400
task_index_lines = 20            # unfinished task files (memory/state/tasks/) listed at the start of every run

[context]
compact = true                   # summarise the old part of a long conversation instead of dropping it
compact_at_tokens = 40000        # ... once what the model sees is estimated above this
keep_recent_tokens = 10000       # ... keeping about this much of the newest part word for word
# model = "gpt-6-luna"           # writes the summary; default: model.fast

[dreaming]
enabled = true                    # a nightly pass that tidies memory from what happened that day
at = "03:30"                     # local time, once a day (a sleeping computer does it on waking)
min_new_items = 20               # quiet days are skipped
max_cost_usd = 0.30              # one pass is stopped above this

[runtime]
concurrency = 2                  # conversations worked on at once
run_timeout_s = 900              # a run longer than this is stopped
tool_timeout_s = 120             # a single tool call longer than this is abandoned
shutdown_grace_s = 30            # on stop, runs in flight get this long to finish

[policy]
mode = "supervised"              # read_only | supervised | auto | autonomous
# always_ask = ["shell:*"]       # patterns that ask in every mode except read_only
# allow = ["shell:git status*"]  # patterns that are pre-approved
# deny = ["shell:rm -rf *"]      # patterns that are never allowed
# After a run has read outside content, new web addresses and other actions that change things ask
# first. These patterns are the exceptions you trust (a pattern is tool or tool:detail):
# allow_when_tainted = ["web_fetch:https://docs.python.org/*", "http_request:GET https://api.open-meteo.com/*"]
approval_timeout_hours = 12      # an unanswered request counts as a refusal

[budget]
per_run_usd = 1.0                # one run above this is stopped and reported
daily_usd = 5.0                  # all runs since local midnight; reaching it pauses work, loses nothing

[notify]
channel = "auto"                 # where unprompted messages go: auto (the phone chat app you last wrote from) | cli | telegram | web
# quiet_hours = "23:00-08:00"    # local time; only urgent messages get through
max_per_hour = 6                 # non-urgent messages per channel per hour

[scheduler]
misfire_grace_s = 3600           # how late a recurring job may run after the computer slept
min_interval_s = 300             # shortest allowed gap between firings of a recurring job
max_timers = 50

[sandbox]
backend = "auto"                 # auto | seatbelt | docker | none  (shell is off if none works)
# image = "python:3.13-slim"     # docker backend: `docker pull` it yourself
# extra_read = ["~/.local/share/uv"]   # more directories commands may read (never write)

[tools]
files = true                     # list/read/write/edit files in the workspace
shell = true                     # needs a working sandbox
web_fetch = true                 # read web pages; never reaches your own network (see [net])
http_request = true              # call APIs; anything but GET/HEAD asks first
web_search = "auto"              # OpenAI hosted search; auto | cached | off

[skills]
enabled = true                   # offer what `pa skills install` put in ~/.personal-agent/skills
# description_chars = 300        # length of each skill's one-line description in the list
# catalog_chars = 8000           # size of that list; the rest are shown by name only

[media]
images = true                    # pictures you send are shown to the model
images_in_context = 2            # each request carries only your newest N pictures
voice = true                     # voice messages are turned into text first (OpenAI speech-to-text)
transcribe_model = "gpt-transcribe"
# language = "zh"                # ISO-639-1 hint; default: detect

# Chat with the agent from Telegram. Create a bot with @BotFather, put its token in .env as
# TELEGRAM_BOT_TOKEN, set enabled = true and run `pa serve`. Message the bot once: it answers
# with a code that you approve here, on your computer: `pa channels approve telegram CODE`.
# Strangers get nothing but a code and never reach the model.
# [telegram]
# enabled = true
# proxy = "socks5://127.0.0.1:1080"    # if Telegram is not reachable directly

# A chat page for any browser, installable on a phone's home screen. It listens on this computer
# only; `tailscale serve --bg 8765` makes it reachable from your own devices over HTTPS. Pair a
# browser with `pa web pair` (a link and QR code that work once). Nobody else can open it.
# [web]
# enabled = true
# public_url = "https://my-mac.tailnet-name.ts.net"   # where your phone reaches it

# Let other programs wake the agent (GitHub, a monitoring tool, your own script). Each route needs
# a secret in .env; the event is treated as outside data, and `prompt` says what to do with it.
# This listener is separate from the chat page so that only this one needs to face the internet.
# [webhooks]
# enabled = true
# [webhooks.routes.github]
# scheme = "github"                    # github | gitlab | standard | generic
# secret_env = "GITHUB_WEBHOOK_SECRET"
# prompt = "Tell me only about failing checks and reviews requested from me."
# events = ["check_suite", "pull_request_review"]

# MCP servers add tools (files, GitHub, a database, ...). They run with your privileges, outside the
# shell sandbox, so add only servers you trust. Nothing is offered until `pa mcp trust NAME` has
# shown you what the server's tools say about themselves. Credentials go in .env as ${NAME}.
# [[mcp.servers]]
# name = "filesystem"
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]
# read_only = ["read_*", "list_*", "search_*", "get_*"]   # these count as reads; the rest ask first
#
# [[mcp.servers]]
# name = "docs"
# transport = "http"
# url = "https://example.com/mcp"
# headers = { Authorization = "Bearer ${DOCS_TOKEN}" }

# [plugins]
# enabled = []                   # Python packages (entry point group personal_agent.plugins) allowed to add tools

# [net]
# allow_private = ["homeassistant.local", "192.168.1.0/24"]   # your own network is blocked unless listed
# timeout_s = 20
# max_response_bytes = 2000000
# max_redirects = 5

# Secrets the agent can *use* but never *see*: it writes {{secret:github}} in an http_request,
# and the value is filled in only for the listed hosts. The value itself stays in .env.
# [secrets.github]
# env = "GITHUB_TOKEN"
# hosts = ["api.github.com"]
"""


DEFAULT_ENV = """\
# Secrets for personal-agent. This file is private (mode 600) and never leaves this machine.
# Put your key after the equals sign. Do not paste it into chat or commit it.
OPENAI_API_KEY=

# Optional: an OpenAI-compatible endpoint instead of api.openai.com
# OPENAI_BASE_URL=
"""


class ConfigError(Exception):
    pass


def read_text_file(path: Path) -> str:
    """The text of a file the owner edits by hand, or a `ConfigError` that says what to do.

    An editor that saves "ANSI" or GBK instead of UTF-8 is the usual reason a file cannot be
    read; a byte-order mark (Notepad's "UTF-8 with BOM") is accepted."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"cannot read {path}: it is not UTF-8 text (byte {exc.start} is not); "
            "save the file as UTF-8"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def load_config(paths: Paths) -> Config:
    """Read `config.toml` (optional) and validate it."""
    data: dict[str, Any] = {}
    if paths.config_file.exists():
        try:
            data = tomllib.loads(read_text_file(paths.config_file))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"cannot read {paths.config_file}: {exc}") from exc
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid {paths.config_file}:\n{_describe(exc)}") from None


def _describe(exc: ValidationError) -> str:
    """One line per problem: where it is and what is wrong. The value that was given is left out:
    a secret pasted into the wrong setting must not end up on a terminal or in a log."""
    lines = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"  {where}: {error['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


def load_env(paths: Paths) -> None:
    """Load `$PA_HOME/.env` into the process environment (never overriding real env vars).

    Only that one file is read: a `.env` in whatever directory the CLI happens to run in
    may belong to an unrelated project and must not leak into this agent.
    """
    if paths.env_file.exists():
        values = dotenv_values(stream=io.StringIO(read_text_file(paths.env_file)))
        for name, value in values.items():
            if value is not None:
                os.environ.setdefault(name, value)


def api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or None
