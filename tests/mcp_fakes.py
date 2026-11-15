"""In-process stand-ins for MCP servers: fast, scriptable, and able to misbehave on demand."""

from __future__ import annotations

from typing import Any

from agents.mcp import MCPServer
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from personal_agent.config import McpServerConfig
from personal_agent.plugins.mcp import ServerFactory, Settings


def tool(
    name: str,
    description: str = "",
    properties: dict[str, Any] | None = None,
    *,
    read_only: bool | None = None,
    destructive: bool | None = None,
) -> Tool:
    annotations = None
    if read_only is not None or destructive is not None:
        annotations = ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive)
    return Tool(
        name=name,
        description=description or f"The {name} tool.",
        input_schema={"type": "object", "properties": properties or {}},
        annotations=annotations,
    )


def answer(value: str, *, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=value)], is_error=error)


class FakeServer(MCPServer):
    def __init__(
        self,
        name: str = "fake",
        tools: list[Tool] | None = None,
        answers: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._name = name
        self.tools: list[Tool] = list(tools or [])
        self.answers = answers or {}
        """Tool name -> CallToolResult, a function of the arguments, or an exception to raise."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connects = 0
        self.cleanups = 0
        self.listings = 0
        self.connect_error: BaseException | None = None
        self.list_error: BaseException | None = None

    @property
    def name(self) -> str:
        return self._name

    async def connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def cleanup(self) -> None:
        self.cleanups += 1

    async def list_tools(self, run_context=None, agent=None) -> list[Tool]:
        self.listings += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.tools)

    async def list_prompts(self, run_context=None, agent=None):
        raise NotImplementedError

    async def get_prompt(self, name, arguments=None):
        raise NotImplementedError

    async def call_tool(self, tool_name, arguments, meta=None) -> CallToolResult:
        self.calls.append((tool_name, dict(arguments or {})))
        result = self.answers.get(tool_name, answer(f"{tool_name} ran"))
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result(arguments or {})
        return result


def factory_for(
    *servers: FakeServer,
) -> tuple[ServerFactory, list[tuple[McpServerConfig, Settings]]]:
    """A server factory that hands out the given fakes by name, and records what it was asked."""
    by_name = {s.name: s for s in servers}
    asked: list[tuple[McpServerConfig, Settings]] = []

    def make(cfg: McpServerConfig, settings: Settings) -> MCPServer:
        asked.append((cfg, settings))
        return by_name[cfg.name]

    return make, asked
