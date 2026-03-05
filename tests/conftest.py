"""Shared test fixtures and helpers for api-agent test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api_agent.context import RequestContext
from api_agent.llm.provider import LLMProvider
from api_agent.llm.types import LLMResponse, ToolCall

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeLLMProvider(LLMProvider):
    """Test double that returns canned LLMResponse objects without HTTP calls.

    Supports multi-turn tool call sequences by iterating through a list of responses.
    """

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(model="fake", api_key="fake-key", provider_name="fake")
        self._responses = list(responses)
        self._call_index = 0
        self.call_log: list[dict] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.call_log.append({"messages": messages, "tools": tools})
        if self._call_index >= len(self._responses):
            return LLMResponse(content="<exhausted>")
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def format_tools(self, tool_defs):
        return [{"name": td.name, "params": td.parameters} for td in tool_defs]

    def format_tool_results(self, tool_results, messages):
        for tr in tool_results:
            messages.append(
                {"role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content}
            )
        return messages

    def format_assistant_tool_calls(self, response, messages):
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                ],
            }
        )
        return messages


def make_text_response(content: str) -> LLMResponse:
    """Create a simple text LLMResponse (no tool calls)."""
    return LLMResponse(content=content)


def make_tool_call_response(
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str = "call_001",
) -> LLMResponse:
    """Create an LLMResponse with a single tool call."""
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=arguments)])


def load_fixture(filename: str) -> dict:
    """Load a JSON fixture file from tests/fixtures/."""
    return json.loads((FIXTURES_DIR / filename).read_text())


@pytest.fixture
def graphql_ctx() -> RequestContext:
    """Standard GraphQL request context for tests."""
    return RequestContext(
        target_url="https://example.com/graphql",
        target_headers={"Authorization": "Bearer test-token"},
        api_type="graphql",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


@pytest.fixture
def rest_ctx() -> RequestContext:
    """Standard REST request context for tests."""
    return RequestContext(
        target_url="https://example.com/openapi.json",
        target_headers={"Authorization": "Bearer test-token"},
        api_type="rest",
        base_url="https://api.example.com/v1",
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


@pytest.fixture
def rest_ctx_with_polling() -> RequestContext:
    """REST context with polling enabled."""
    return RequestContext(
        target_url="https://example.com/openapi.json",
        target_headers={},
        api_type="rest",
        base_url="https://api.example.com/v1",
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=("/async/search",),
    )


@pytest.fixture
def fake_provider_factory():
    """Factory fixture to create and monkeypatch a FakeLLMProvider."""

    def _create(first, second=None) -> FakeLLMProvider:
        # Support both calling conventions:
        #   fake_provider_factory(responses, monkeypatch)
        #   fake_provider_factory(monkeypatch, responses)
        if second is None:
            raise TypeError(
                "fake_provider_factory requires (responses, monkeypatch) or (monkeypatch, responses)"
            )
        if isinstance(first, list):
            responses, mp = first, second
        else:
            mp, responses = first, second
        provider = FakeLLMProvider(responses)
        mp.setattr("api_agent.agent.model._provider", provider)
        mp.setattr("api_agent.agent.model.provider", provider)
        return provider

    return _create


# Introspection response matching the sample schema fixture
SAMPLE_INTROSPECTION_RESPONSE = {
    "data": {
        "__schema": {
            "queryType": {
                "fields": [
                    {
                        "name": "users",
                        "description": "Get all users",
                        "args": [
                            {
                                "name": "limit",
                                "type": {"kind": "SCALAR", "name": "Int", "ofType": None},
                                "defaultValue": None,
                            },
                            {
                                "name": "offset",
                                "type": {"kind": "SCALAR", "name": "Int", "ofType": None},
                                "defaultValue": None,
                            },
                        ],
                        "type": {
                            "kind": "NON_NULL",
                            "name": None,
                            "ofType": {
                                "kind": "LIST",
                                "name": None,
                                "ofType": {
                                    "kind": "NON_NULL",
                                    "name": None,
                                    "ofType": {"kind": "OBJECT", "name": "User"},
                                },
                            },
                        },
                    },
                    {
                        "name": "user",
                        "description": "Get user by ID",
                        "args": [
                            {
                                "name": "id",
                                "type": {
                                    "kind": "NON_NULL",
                                    "name": None,
                                    "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                                },
                                "defaultValue": None,
                            },
                        ],
                        "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                    },
                ]
            },
            "types": [
                {
                    "name": "User",
                    "kind": "OBJECT",
                    "description": "A user account",
                    "fields": [
                        {
                            "name": "id",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                            },
                        },
                        {
                            "name": "name",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
                            },
                        },
                        {
                            "name": "email",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
                            },
                        },
                    ],
                    "enumValues": None,
                    "inputFields": None,
                    "interfaces": [],
                    "possibleTypes": None,
                },
                {
                    "name": "Post",
                    "kind": "OBJECT",
                    "description": "A blog post",
                    "fields": [
                        {
                            "name": "id",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                            },
                        },
                        {
                            "name": "title",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
                            },
                        },
                        {
                            "name": "authorId",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                            },
                        },
                        {
                            "name": "views",
                            "description": None,
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "Int", "ofType": None},
                            },
                        },
                    ],
                    "enumValues": None,
                    "inputFields": None,
                    "interfaces": [],
                    "possibleTypes": None,
                },
            ],
        }
    }
}
