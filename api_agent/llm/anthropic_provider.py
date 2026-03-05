"""Anthropic (Claude) provider — native SDK with tool_use content blocks."""

from __future__ import annotations

from typing import Any

import anthropic

from .provider import LLMProvider
from .types import LLMResponse, ToolCall, ToolDefinition, ToolResult


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider using native SDK."""

    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    def __init__(self, model: str | None = None, api_key: str = "", base_url: str | None = None):
        super().__init__(model or self.DEFAULT_MODEL, api_key, base_url, provider_name="anthropic")
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        # Extract system message — Anthropic uses a separate parameter
        system_content = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg["content"]
            else:
                api_messages.append(msg)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_content:
            kwargs["system"] = system_content
        if tools:
            kwargs["tools"] = tools

        response = await self.client.messages.create(**kwargs)

        # Parse response content blocks
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            }

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
        )

    def format_tools(self, tool_defs: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert to Anthropic tool format."""
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.parameters,
            }
            for td in tool_defs
        ]

    def format_tool_results(
        self, tool_results: list[ToolResult], messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Anthropic expects tool results as a user message with tool_result content blocks."""
        content = [
            {
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.content,
            }
            for tr in tool_results
        ]
        messages.append({"role": "user", "content": content})
        return messages

    def format_assistant_tool_calls(
        self, response: LLMResponse, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Anthropic assistant messages contain tool_use content blocks."""
        content: list[dict[str, Any]] = []

        # Include any text content
        if response.content:
            content.append({"type": "text", "text": response.content})

        # Add tool_use blocks
        for tc in response.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )

        messages.append({"role": "assistant", "content": content})
        return messages
