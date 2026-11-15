"""User configuration (`config.toml`) and environment loading.

Secrets never live in `config.toml`; they come from the environment / `.env`.
Unknown keys are rejected so typos are noticed instead of silently ignored.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_agent.clock import parse_quiet_hours
from personal_agent.paths import Paths

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


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
    channel: str = "cli"
    """Where messages go when no conversation is waiting (scheduled work, alerts)."""
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


ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""`${NAME}` in an MCP server's `env` or `headers`: filled in from the agent's environment (`.env`)
when the server starts, so a credential never has to be written into config.toml."""

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


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
timeout_s = 180
max_retries = 3

[memory]
max_file_bytes = 65536
max_total_bytes = 4194304
max_files = 500
view_max_chars = 16000
index_max_lines = 200            # lines of memory/state/MEMORY.md injected into every run
index_max_bytes = 25000

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
channel = "cli"                  # where unprompted messages go
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


def load_config(paths: Paths) -> Config:
    """Read `config.toml` (optional) and validate it."""
    data: dict = {}
    if paths.config_file.exists():
        try:
            data = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {paths.config_file}: {exc}") from exc
    try:
        return Config.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError -> readable message
        raise ConfigError(f"invalid {paths.config_file}:\n{exc}") from exc


def load_env(paths: Paths) -> None:
    """Load `$PA_HOME/.env` into the process environment (never overriding real env vars).

    Only that one file is read: a `.env` in whatever directory the CLI happens to run in
    may belong to an unrelated project and must not leak into this agent.
    """
    if paths.env_file.exists():
        load_dotenv(paths.env_file, override=False)


def api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or None
