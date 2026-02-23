"""Shared types for the LLM provider layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict  # parsed JSON arguments


@dataclass
class ToolResult:
    """Result of executing a tool."""

    tool_call_id: str
    name: str
    content: str  # serialized result string


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolDefinition:
    """Provider-agnostic tool definition."""

    name: str
    description: str
    parameters: dict  # JSON Schema object
    function: object  # the callable (sync or async)
