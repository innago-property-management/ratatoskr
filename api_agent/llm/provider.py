"""Base LLM provider with the tool-calling loop."""

from __future__ import annotations

import inspect
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

import structlog

from ..exceptions import APIAgentError
from .types import LLMResponse, ToolCall, ToolDefinition, ToolResult

logger = structlog.get_logger(__name__)

# Sentinel object for direct-return signaling.  Compared by identity (``is``),
# not equality, so it can never collide with legitimate LLM output text.
DIRECT_RETURN: object = object()


@dataclass
class RunResult:
    """Result of a tool-calling loop run."""

    final_output: str | object | None
    tool_results: list[ToolResult]
    turns_used: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class MaxTurnsExceeded(APIAgentError):
    """Raised when the tool-calling loop exceeds max_turns."""

    def __init__(self, turns: int, last_result: RunResult):
        self.turns = turns
        self.last_result = last_result
        super().__init__(f"Max turns ({turns}) exceeded")


class LLMProvider(ABC):
    """Abstract LLM provider. Subclasses implement complete(); the tool loop is shared."""

    def __init__(
        self, model: str, api_key: str, base_url: str | None = None, provider_name: str = "unknown"
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.provider_name = provider_name

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send a chat completion request. Returns normalized LLMResponse."""
        ...

    @abstractmethod
    def format_tools(self, tool_defs: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert ToolDefinitions to provider-specific tool format."""
        ...

    @abstractmethod
    def format_tool_results(
        self, tool_results: list[ToolResult], messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Append tool results to message list in provider-specific format.

        Contract: implementations MUST return the ``messages`` list (mutated
        in-place).  Callers MUST use the return value, not rely on mutation
        alone, so that a future implementation may return a *new* list.
        """
        ...

    @abstractmethod
    def format_assistant_tool_calls(
        self, response: LLMResponse, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Append assistant's tool-call message to the message list.

        Contract: implementations MUST return the ``messages`` list (mutated
        in-place).  Callers MUST use the return value, not rely on mutation
        alone, so that a future implementation may return a *new* list.
        """
        ...

    async def run_tool_loop(
        self,
        *,
        instructions: str,
        user_message: str,
        tool_defs: list[ToolDefinition],
        max_turns: int = 30,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        should_stop: Callable[[list[ToolResult]], bool] | None = None,
        inject_instructions: Callable[[str, int], str] | None = None,
    ) -> RunResult:
        """Run the tool-calling loop.

        Args:
            instructions: System prompt
            user_message: Initial user message
            tool_defs: Available tools
            max_turns: Maximum LLM calls before raising MaxTurnsExceeded
            temperature: LLM temperature
            max_tokens: Max tokens per LLM call
            should_stop: Optional callback — if returns True after tool execution, stop loop
            inject_instructions: Optional callback to mutate instructions before each LLM call
                                 (turn_number) → modified_instructions
        """
        tools_map = {td.name: td for td in tool_defs}
        formatted_tools = self.format_tools(tool_defs) if tool_defs else None

        messages = self._build_initial_messages(instructions, user_message)

        all_tool_results: list[ToolResult] = []
        turns = 0
        prompt_tokens_acc = 0
        completion_tokens_acc = 0
        total_tokens_acc = 0

        def _accumulate_usage(resp: LLMResponse) -> None:
            nonlocal prompt_tokens_acc, completion_tokens_acc, total_tokens_acc
            if resp.usage:
                prompt_tokens_acc += resp.usage.get("prompt_tokens", 0)
                completion_tokens_acc += resp.usage.get("completion_tokens", 0)
                total_tokens_acc += resp.usage.get("total_tokens", 0)

        while turns < max_turns:
            turns += 1

            # Optionally mutate system instructions per turn
            if inject_instructions and messages and messages[0].get("role") == "system":
                messages[0]["content"] = inject_instructions(instructions, turns)

            response = await self.complete(
                messages, tools=formatted_tools, temperature=temperature, max_tokens=max_tokens
            )
            _accumulate_usage(response)

            if not response.has_tool_calls:
                return RunResult(
                    final_output=response.content,
                    tool_results=all_tool_results,
                    turns_used=turns,
                    prompt_tokens=prompt_tokens_acc,
                    completion_tokens=completion_tokens_acc,
                    total_tokens=total_tokens_acc,
                )

            # Append assistant message with tool calls
            messages = self.format_assistant_tool_calls(response, messages)

            # Execute tools
            turn_results = await self._execute_tools(response.tool_calls, tools_map)
            all_tool_results.extend(turn_results)

            # Check early stop
            if should_stop and should_stop(turn_results):
                return RunResult(
                    final_output=DIRECT_RETURN,
                    tool_results=all_tool_results,
                    turns_used=turns,
                    prompt_tokens=prompt_tokens_acc,
                    completion_tokens=completion_tokens_acc,
                    total_tokens=total_tokens_acc,
                )

            # Append tool results
            messages = self.format_tool_results(turn_results, messages)

        raise MaxTurnsExceeded(
            max_turns,
            RunResult(
                final_output=None,
                tool_results=all_tool_results,
                turns_used=turns,
                prompt_tokens=prompt_tokens_acc,
                completion_tokens=completion_tokens_acc,
                total_tokens=total_tokens_acc,
            ),
        )

    def _build_initial_messages(self, instructions: str, user_message: str) -> list[dict[str, Any]]:
        """Build initial message list. Override for providers with different system message handling."""
        return [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_message},
        ]

    async def _execute_tools(
        self, tool_calls: list[ToolCall], tools_map: dict[str, ToolDefinition]
    ) -> list[ToolResult]:
        """Execute tool calls and return results."""
        results: list[ToolResult] = []

        for tc in tool_calls:
            td = tools_map.get(tc.name)
            if not td:
                results.append(
                    ToolResult(
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=json.dumps({"error": f"Unknown tool: {tc.name}"}),
                    )
                )
                continue

            try:
                fn = td.function
                if inspect.iscoroutinefunction(fn):
                    result = await fn(**tc.arguments)
                else:
                    result = fn(**tc.arguments)

                content = result if isinstance(result, str) else json.dumps(result)
            except Exception as e:
                logger.exception("tool_execution_failed", tool=tc.name)
                content = json.dumps({"error": str(e)})

            results.append(ToolResult(tool_call_id=tc.id, name=tc.name, content=content))

        return results
