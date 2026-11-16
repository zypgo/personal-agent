"""The MCP hub: connects the owner's MCP servers and offers their tools like any other tool.

What the hub does, and what it refuses to do:

* A tool of an MCP server is registered under `mcp__<server>__<tool>` and goes through the same
  `ToolGuard` as every built-in tool: policy, approval, audit, timeout, output limits. The server
  is never asked anything the guard has not allowed.
* The model sees a tool only while its definition is the one the owner reviewed (`McpPins`). A
  new or changed tool waits for `pa mcp trust`, because a description is text the model obeys.
* Results are outside content: fenced as data, and a run that read one asks before acting.
  Images and other binary content are left out.
* Servers run with the owner's privileges and only the environment the owner listed. They are
  not sandboxed (the shell sandbox is for commands the model writes, not for programs the owner
  chose to run), and they can fail: a server that does not start or stops answering costs its own
  tools and a note to the model, never the run.
* Sampling, elicitation and roots are not offered to servers, so a server cannot make the agent
  call a model or ask questions on its own.

One hub belongs to one event loop (the connections do). Every place that runs a loop to its end
calls `stop()` first; a hub that finds itself in another loop drops what it had and starts fresh.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import FunctionTool
from agents.mcp import (
    MCPServer,
    MCPServerManager,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
)
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from personal_agent.clock import Clock
from personal_agent.config import ENV_REFERENCE, McpConfig, McpServerConfig, RuntimeConfig
from personal_agent.db import Database
from personal_agent.harness.instructions import wrap_untrusted
from personal_agent.parsing import loads_json
from personal_agent.paths import Paths
from personal_agent.plugins.mcp_convert import (
    describe,
    matches_any,
    parameters_schema,
    public_name,
    render_result,
)
from personal_agent.plugins.mcp_pins import Definition, McpPins, Review, definition_of, digest_of
from personal_agent.security.audit import safe_args
from personal_agent.security.redact import known_secret_values, redact, register_secret_envs
from personal_agent.tools.base import Category, ToolDef, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 600.0
CLEANUP_TIMEOUT_S = 10.0
MAX_LIST_PAGES = 20
LOG_TAIL_BYTES = 4_000


class McpStartError(Exception):
    """A server could not be started; the message says why, in words for the owner."""


@dataclass(frozen=True)
class Settings:
    """A server's configuration with `${NAME}` filled in. Never printed or logged."""

    env: dict[str, str]
    headers: dict[str, str]
    log_path: Path
    args: list[str] = field(default_factory=list)
    url: str = ""


ServerFactory = Callable[[McpServerConfig, Settings], MCPServer]


# ---- creating the SDK's server objects -------------------------------------------------------
@asynccontextmanager
async def _streams_with_log(params: Any, log_path: Path) -> AsyncIterator[Any]:
    """The program's own messages (stderr) go to a private log file, not over the chat."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Appending, so that `logs.trim_logs` can cut a long log down without the program that writes to
    # it carrying on at the old length (which would leave a hole of nothing before its next line).
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            yield streams


class _LoggedStdio(MCPServerStdio):
    def __init__(self, *args: Any, log_path: Path, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._log_path = log_path

    def create_streams(self) -> AbstractAsyncContextManager[Any]:
        return _streams_with_log(self.params, self._log_path)


def default_server_factory(cfg: McpServerConfig, settings: Settings) -> MCPServer:
    session_timeout = max(cfg.call_timeout_s, cfg.connect_timeout_s)
    common: dict[str, Any] = {
        "name": cfg.name,
        "cache_tools_list": False,
        "client_session_timeout_seconds": session_timeout,
        "max_list_pages": MAX_LIST_PAGES,
        # A tool call may have had an effect: it is never repeated behind the model's back.
        "max_retry_attempts": 0,
    }
    if cfg.transport == "stdio":
        params: dict[str, Any] = {"command": cfg.command, "args": settings.args}
        if settings.env:
            params["env"] = settings.env
        if cfg.cwd:
            params["cwd"] = os.path.expanduser(cfg.cwd)
        return _LoggedStdio(params, log_path=settings.log_path, **common)
    http: dict[str, Any] = {
        "url": settings.url,
        "headers": settings.headers or None,
        "timeout": session_timeout,
        "sse_read_timeout": max(300.0, session_timeout),
    }
    if cfg.transport == "sse":
        return MCPServerSse(http, **common)  # type: ignore[arg-type]
    return MCPServerStreamableHttp(http, **common)  # type: ignore[arg-type]


# ---- state the hub keeps per server ------------------------------------------------------------
@dataclass
class ToolInfo:
    """One tool a server lists, with what the hub decided about it."""

    name: str
    definition: Definition
    digest: str
    review: Review
    category: Category
    filtered: str = ""
    """Why the owner's own settings exclude it (`allow_tools`, `deny_tools`)."""
    public: str = ""
    offered: bool = False
    withheld: str = ""
    """Why a tool that passed the filters is still not offered."""

    @property
    def reads_only(self) -> bool:
        """It only looks at things, so it runs without asking."""
        return self.category is Category.READ


@dataclass
class ServerState:
    config: McpServerConfig
    status: str = "stopped"  # stopped | connected | failed | review
    error: str = ""
    server: MCPServer | None = None
    manager: MCPServerManager | None = None
    failures: int = 0
    retry_at: float = 0.0
    listed_at: float = 0.0
    suspect: bool = False
    """A call failed: the next `prepare` checks the connection before trusting it."""
    pins_seen: str = ""
    """Fingerprint of the reviewed set the tools were last judged against."""
    raw_tools: list[Any] = field(default_factory=list)
    tools: list[ToolInfo] = field(default_factory=list)


@dataclass(frozen=True)
class ServerReport:
    name: str
    transport: str
    status: str
    error: str
    tools: list[ToolInfo]
    log_tail: str = ""


@dataclass(frozen=True)
class ServerSummary:
    """What is known without connecting (for `pa mcp list` and `pa doctor`)."""

    name: str
    transport: str
    target: str
    enabled: bool
    pin: str
    reviewed_tools: int


def describe_error(exc: BaseException | None, timeout: float) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if exc is None:
        return "it could not be started"
    if isinstance(exc, TimeoutError):
        return f"it did not answer within {timeout:g} seconds"
    if isinstance(exc, asyncio.CancelledError):
        return f"it did not finish starting within {timeout:g} seconds"
    if isinstance(exc, FileNotFoundError):
        return f"the program was not found ({exc})"
    if isinstance(exc, McpStartError):
        return str(exc)
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}"[:300]


class McpHub:
    def __init__(
        self,
        config: McpConfig,
        *,
        paths: Paths,
        db: Database,
        clock: Clock,
        registry: ToolRegistry,
        runtime: RuntimeConfig,
        environ: Mapping[str, str] | None = None,
        factory: ServerFactory | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.clock = clock
        self.registry = registry
        self.runtime = runtime
        self.pins = McpPins(db, clock)
        self._environ = environ if environ is not None else os.environ
        self._factory = factory or default_server_factory
        self._states = {s.name: ServerState(s) for s in config.servers}
        self._registered: dict[str, tuple[str, str, str]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        register_secret_envs(name for s in config.servers for name in s.referenced_env())

    # ---- what is configured ----------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.config.enabled and any(s.config.enabled for s in self._states.values())

    def names(self) -> list[str]:
        return list(self._states)

    def summaries(self) -> list[ServerSummary]:
        out = []
        for st in self._states.values():
            cfg = st.config
            target = cfg.command if cfg.transport == "stdio" else cfg.url
            if cfg.transport == "stdio" and cfg.args:
                target = f"{cfg.command} {' '.join(cfg.args)}"
            out.append(
                ServerSummary(
                    name=cfg.name,
                    transport=cfg.transport,
                    target=target or "",
                    enabled=cfg.enabled and self.config.enabled,
                    pin=cfg.pin,
                    reviewed_tools=self.pins.count(cfg.name),
                )
            )
        return out

    def log_path(self, name: str) -> Path:
        return self.paths.logs_dir / f"mcp-{name}.log"

    def log_tail(self, name: str) -> str:
        path = self.log_path(name)
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return redact(text, known_secret_values(self._environ)).strip()

    # ---- the run-time interface ----------------------------------------------------------------
    async def prepare(self) -> None:
        """Make the tools of every usable server available. Cheap when nothing changed; a server
        that cannot be reached is skipped (and retried later), never an error."""
        if not self.active:
            return
        self._bind()
        assert self._lock is not None
        async with self._lock:
            now = self.clock.now()
            states = [s for s in self._states.values() if s.config.enabled]
            await asyncio.gather(*(self._ensure(s, now) for s in states))
            self._sync_registry()

    async def stop(self) -> None:
        """Close every connection. Call before the event loop ends."""
        if self._loop is None:
            return
        if self._loop is not asyncio.get_running_loop():
            self._bind()  # connections of another loop cannot be closed from here
            self._loop = None
            return
        assert self._lock is not None
        async with self._lock:
            await asyncio.gather(*(self._drop(s) for s in self._states.values()))
            self._unregister_all()
            for state in self._states.values():
                state.status = "stopped"
                state.tools = []
                state.raw_tools = []
        self._loop = None
        self._lock = None

    def notes(self) -> list[str]:
        """What the model should know about servers it cannot use right now."""
        notes: list[str] = []
        if not self.active:
            return notes
        for st in self._states.values():
            if not st.config.enabled:
                continue
            name = st.config.name
            if st.status == "failed":
                notes.append(
                    f"MCP server '{name}' is not available; its tools are not offered in this run. "
                    f"The owner can inspect it with `pa mcp tools {name}`."
                )
            elif st.status == "review":
                notes.append(
                    f"MCP server '{name}' has tools waiting for the owner's review "
                    f"(`pa mcp trust {name}`); none are offered yet."
                )
                continue
            waiting = [
                t for t in st.tools if not t.filtered and t.review in (Review.NEW, Review.CHANGED)
            ]
            if waiting:
                notes.append(
                    f"MCP server '{name}': {len(waiting)} tool(s) are new or changed since the owner "
                    f"reviewed them and are not offered (`pa mcp trust {name}`)."
                )
        return notes

    def unavailable(self, name: str) -> str | None:
        st = self._states.get(name)
        if st is None or st.status != "connected":
            return f"the MCP server {name!r} is not connected"
        return None

    # ---- the owner-facing interface (`pa mcp`) -----------------------------------------------
    async def inspect(self, name: str) -> ServerReport:
        """Connect to one server now (even one that is switched off or not reviewed) and report."""
        st = self._state(name)
        self._bind()
        assert self._lock is not None
        async with self._lock:
            if st.status != "connected":
                await self._connect(st, self.clock.now())
            self._classify_all()
            return self._report(st)

    def pin(self, name: str, report: ServerReport | None = None, *, by: str = "owner") -> int:
        """The owner has read these tools: from now on those definitions are the trusted ones.

        Pass the report that was shown to them, so that exactly what they read is what gets
        trusted, even if the server has changed its mind since."""
        st = self._state(name)
        shown = report.tools if report is not None else st.tools
        count = self.pins.replace(name, [t.definition for t in shown if not t.filtered], by=by)
        st.tools = self._classify(st, st.raw_tools)
        if st.status == "review":
            st.status = "stopped"
        return count

    def report(self, name: str) -> ServerReport:
        return self._report(self._state(name))

    # ---- connecting ------------------------------------------------------------------------
    def _state(self, name: str) -> ServerState:
        try:
            return self._states[name]
        except KeyError:
            raise KeyError(f"no MCP server called {name!r}") from None

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is self._loop:
            return
        if self._loop is not None:
            log.warning("MCP connections belonged to another event loop; starting afresh")
            for state in self._states.values():
                state.server = state.manager = None
                state.status = "stopped"
                state.tools = []
            self._unregister_all()
        self._loop = loop
        self._lock = asyncio.Lock()

    def _report(self, st: ServerState) -> ServerReport:
        return ServerReport(
            name=st.config.name,
            transport=st.config.transport,
            status=st.status,
            error=st.error,
            tools=list(st.tools),
            log_tail=self.log_tail(st.config.name) if st.config.transport == "stdio" else "",
        )

    async def _ensure(self, st: ServerState, now: float) -> None:
        if st.status == "connected":
            if self.pins.fingerprint(st.config.name) != st.pins_seen:
                st.tools = self._classify(st, st.raw_tools)  # the owner reviewed something
            if st.suspect or now - st.listed_at >= self.config.refresh_s:
                try:
                    await self._relist(st, now)
                except Exception as exc:
                    await self._fail(st, now, exc)
            return
        if now < st.retry_at:
            return
        if st.config.pin == "ask" and self.pins.count(st.config.name) == 0:
            st.status = "review"  # nothing could be offered, so do not even start the program
            st.error = ""
            return
        await self._connect(st, now)

    async def _connect(self, st: ServerState, now: float) -> None:
        cfg = st.config
        try:
            settings = self._resolve(cfg)
            server = self._factory(cfg, settings)
            manager = MCPServerManager(
                [server],
                connect_timeout_seconds=cfg.connect_timeout_s,
                cleanup_timeout_seconds=CLEANUP_TIMEOUT_S,
                drop_failed_servers=True,
                strict=False,
                connect_in_parallel=True,  # every connection lives in one task from start to end
            )
            await manager.connect_all()
            if server not in manager.active_servers:
                error = manager.errors.get(server)
                await self._cleanup(manager)
                raise McpStartError(describe_error(error, cfg.connect_timeout_s))
            st.server, st.manager = server, manager
            await self._relist(st, now)
        except asyncio.CancelledError:
            await self._drop(st)
            raise
        except Exception as exc:
            await self._fail(st, now, exc)
            return
        st.status = "connected"
        st.failures = 0
        st.error = ""
        st.retry_at = 0.0

    def _resolve(self, cfg: McpServerConfig) -> Settings:
        def expand(value: str) -> str:
            for ref in ENV_REFERENCE.findall(value):
                if not self._environ.get(ref):
                    raise McpStartError(f"{ref} is not set (put it in {self.paths.env_file})")
            return ENV_REFERENCE.sub(lambda m: self._environ[m.group(1)], value)

        return Settings(
            env={key: expand(value) for key, value in cfg.env.items()},
            headers={key: expand(value) for key, value in cfg.headers.items()},
            log_path=self.log_path(cfg.name),
            args=[expand(arg) for arg in cfg.args],
            url=expand(cfg.url or ""),
        )

    async def _relist(self, st: ServerState, now: float) -> None:
        assert st.server is not None
        tools = await asyncio.wait_for(st.server.list_tools(), st.config.connect_timeout_s)
        st.raw_tools = list(tools)
        st.tools = self._classify(st, st.raw_tools)
        st.listed_at = now
        st.suspect = False

    async def _fail(self, st: ServerState, now: float, exc: BaseException) -> None:
        st.failures += 1
        text = describe_error(exc, st.config.connect_timeout_s)
        st.error = redact(text, known_secret_values(self._environ))
        st.status = "failed"
        st.tools = []
        st.raw_tools = []
        st.retry_at = now + min(MAX_BACKOFF_S, self.config.retry_s * 2 ** (st.failures - 1))
        log.warning("MCP server %s is not available: %s", st.config.name, st.error)
        await self._drop(st)

    async def _drop(self, st: ServerState) -> None:
        manager, st.manager, st.server = st.manager, None, None
        if manager is not None:
            await self._cleanup(manager)

    @staticmethod
    async def _cleanup(manager: MCPServerManager) -> None:
        try:
            await asyncio.wait_for(manager.cleanup_all(), CLEANUP_TIMEOUT_S + 5)
        except Exception as exc:
            log.warning("could not close an MCP server cleanly: %s", describe_error(exc, 0))

    # ---- deciding what is offered ------------------------------------------------------------
    def _category(self, cfg: McpServerConfig, definition: Definition) -> Category:
        if matches_any(definition["name"], cfg.read_only):
            return Category.READ
        annotations = definition["annotations"]
        if cfg.trust_annotations and annotations.get("readOnlyHint") is True:
            return Category.READ
        # Everything else may change something. A program on this machine runs with the owner's
        # rights; a web service receives whatever the model sends it.
        return Category.EXEC if cfg.transport == "stdio" else Category.NET_WRITE

    def _classify(self, st: ServerState, raw_tools: list[Any]) -> list[ToolInfo]:
        cfg = st.config
        definitions = [definition_of(t) for t in raw_tools]
        wanted = [
            d
            for d in definitions
            if (not cfg.allow_tools or matches_any(d["name"], cfg.allow_tools))
            and not matches_any(d["name"], cfg.deny_tools)
        ]
        pins = self.pins.for_server(cfg.name)
        if cfg.pin == "auto" and not pins and wanted:
            self.pins.replace(cfg.name, wanted, by="auto")
            pins = self.pins.for_server(cfg.name)
        st.pins_seen = self.pins.fingerprint(cfg.name)
        infos: list[ToolInfo] = []
        for definition in definitions:
            name = definition["name"]
            info = ToolInfo(
                name=name,
                definition=definition,
                digest=digest_of(definition),
                review=self.pins.review(pins, definition),
                category=self._category(cfg, definition),
            )
            if cfg.allow_tools and not matches_any(name, cfg.allow_tools):
                info.filtered = "not in allow_tools"
            elif matches_any(name, cfg.deny_tools):
                info.filtered = "in deny_tools"
            elif info.review is not Review.TRUSTED:
                info.withheld = {
                    Review.UNREVIEWED: "not reviewed yet",
                    Review.NEW: "new since your review",
                    Review.CHANGED: "changed since your review",
                }[info.review]
            else:
                info.offered = True
            infos.append(info)
        return infos

    def _classify_all(self) -> None:
        for st in self._states.values():
            if st.raw_tools:
                st.tools = self._classify(st, st.raw_tools)

    # ---- the tool registry --------------------------------------------------------------------
    def _sync_registry(self) -> None:
        desired: dict[str, tuple[ServerState, ToolInfo]] = {}
        taken: set[str] = {n for n in self.registry.names() if n not in self._registered}
        for st in self._states.values():
            if st.status != "connected" or not st.config.enabled:
                continue
            for info in st.tools:
                if not info.offered:
                    continue
                if len(desired) >= self.config.max_tools:
                    info.offered = False
                    info.withheld = f"over the limit of {self.config.max_tools} MCP tools"
                    continue
                info.public = public_name(st.config.name, info.name, taken | set(desired))
                desired[info.public] = (st, info)

        for name in list(self._registered):
            server, tool, digest = self._registered[name]
            wanted = desired.get(name)
            if wanted is None or (wanted[0].config.name, wanted[1].name, wanted[1].digest) != (
                server,
                tool,
                digest,
            ):
                self.registry.unregister(name)
                del self._registered[name]
        for name, (st, info) in desired.items():
            if name in self._registered:
                continue
            try:
                self.registry.register(self._tool_def(st.config, info))
            except ValueError:
                log.warning("MCP tool %s clashes with another tool and is skipped", name)
                info.offered = False
                info.withheld = "its name clashes with another tool"
                continue
            self._registered[name] = (st.config.name, info.name, info.digest)

    def _unregister_all(self) -> None:
        for name in self._registered:
            self.registry.unregister(name)
        self._registered.clear()

    def _tool_def(self, cfg: McpServerConfig, info: ToolInfo) -> ToolDef:
        hub, server_name, tool_name = self, cfg.name, info.name

        async def invoke(_context: Any, raw_args: str) -> ToolResult:
            return await hub._call(server_name, tool_name, raw_args)

        tool = FunctionTool(
            name=info.public,
            description=describe(info.definition["description"], info.definition["title"]),
            params_json_schema=parameters_schema(info.definition["inputSchema"]),
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )
        return ToolDef(
            name=info.public,
            category=info.category,
            tool=tool,
            profiles=frozenset(cfg.profiles),
            taints=cfg.taints,
            source=f"mcp:{cfg.name}",
            detail=lambda args: safe_args(args),
            review=lambda args: safe_args(args),
            available=lambda _app: hub.unavailable(server_name),
            timeout=lambda _app, _args: cfg.call_timeout_s + 5,
        )

    # ---- calling ------------------------------------------------------------------------------
    async def _call(self, server_name: str, tool_name: str, raw_args: str) -> ToolResult:
        st = self._states[server_name]
        cfg = st.config
        server = st.server
        if server is None or st.status != "connected":
            return ToolResult(
                f"Error: the MCP server {server_name!r} is not connected right now.", error=True
            )
        try:
            args = loads_json(raw_args) if raw_args and raw_args.strip() else {}
        except ValueError:
            return ToolResult("Error: the arguments were not valid JSON.", error=True)
        if not isinstance(args, dict):
            return ToolResult("Error: the arguments must be a JSON object.", error=True)
        try:
            result = await asyncio.wait_for(server.call_tool(tool_name, args), cfg.call_timeout_s)
        except TimeoutError:
            st.suspect = True
            return ToolResult(
                f"Error: {server_name}/{tool_name} did not answer within {cfg.call_timeout_s:g} "
                "seconds. It may have partly run; check the current state before trying again.",
                error=True,
            )
        except MCPError as exc:
            st.suspect = True
            # JSON-RPC errors are authored by the server. Preserve a bounded useful explanation,
            # but give it the same trust boundary as a regular MCP result, even though it failed.
            text = describe_error(exc, cfg.call_timeout_s)
            if cfg.taints:
                text = wrap_untrusted(text, f"mcp:{server_name}")
            return ToolResult(
                f"Error: the server {server_name}/{tool_name} reported a protocol error:\n{text}",
                error=True,
                untrusted=cfg.taints,
            )
        except Exception as exc:
            st.suspect = True
            # Other SDK/transport exceptions may include response bodies too. Keep arbitrary
            # exception text in the owner-facing diagnostics, never in an untainted model result.
            log.warning(
                "MCP call %s/%s failed: %s",
                server_name,
                tool_name,
                redact(describe_error(exc, cfg.call_timeout_s), known_secret_values(self._environ)),
            )
            return ToolResult(
                f"Error: the call to {server_name}/{tool_name} failed ({type(exc).__name__}).",
                error=True,
            )
        room = max(500, self.runtime.tool_output_chars - 800)
        rendered = render_result(result, room=room)
        text = rendered.text
        if cfg.taints:
            text = wrap_untrusted(text, f"mcp:{server_name}")
        if rendered.is_error:
            return ToolResult(
                f"The tool {server_name}/{tool_name} reported a failure:\n{text}",
                error=True,
                untrusted=cfg.taints,
            )
        # Keep the established readable prefix for string-only callers; the guard uses metadata.
        return ToolResult(
            f"Result: {text}" if text.startswith("Error") else text, untrusted=cfg.taints
        )
