"""OpenAI-compatible provider for custom endpoints (Ollama, LM Studio, vLLM, etc.)."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from .provider import LLMProvider
from .types import LLMResponse, ToolCall, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible endpoint provider.

    Same as OpenAI but:
    - base_url is required
    - Gracefully handles missing tool-calling support
    - api_key defaults to "not-needed" for local models
    """

    def __init__(self, model: str, api_key: str = "not-needed", base_url: str | None = None):
        if not base_url:
            raise ValueError("base_url is required for openai-compat provider")
        super().__init__(model, api_key or "not-needed", base_url)
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=base_url)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Only include tools if provided — some endpoints don't support them
        if tools:
            kwargs["tools"] = tools

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # If tool calling fails, retry without tools
            if tools and ("tool" in str(e).lower() or "function" in str(e).lower()):
                logger.warning(f"Endpoint doesn't support tools, retrying without: {e}")
                kwargs.pop("tools", None)
                response = await self.client.chat.completions.create(**kwargs)
            else:
                raise

        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(content=message.content, tool_calls=tool_calls, usage=usage)

    def format_tools(self, tool_defs: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                },
            }
            for td in tool_defs
        ]

    def format_tool_results(
        self, tool_results: list[ToolResult], messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        for tr in tool_results:
            messages.append(
                {"role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content}
            )
        return messages

    def format_assistant_tool_calls(
        self, response: LLMResponse, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        import json as _json

        tool_calls_data = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": _json.dumps(tc.arguments)},
            }
            for tc in response.tool_calls
        ]
        messages.append(
            {"role": "assistant", "content": response.content, "tool_calls": tool_calls_data}
        )
        return messages
