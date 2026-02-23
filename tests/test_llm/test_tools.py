"""Tests for api_agent.llm.tools — @tool decorator."""


from api_agent.llm.tools import tool
from api_agent.llm.types import ToolDefinition


class TestToolDecorator:
    def test_basic_function(self):
        @tool
        def greet(name: str) -> str:
            """Say hello."""
            return f"Hello, {name}"

        assert isinstance(greet, ToolDefinition)
        assert greet.name == "greet"
        assert greet.description == "Say hello."
        assert greet.parameters["type"] == "object"
        assert "name" in greet.parameters["properties"]
        assert greet.parameters["properties"]["name"]["type"] == "string"
        assert greet.parameters["required"] == ["name"]

    def test_multiple_params(self):
        @tool
        def search(query: str, limit: int = 10) -> str:
            """Search for items."""
            return ""

        props = search.parameters["properties"]
        assert "query" in props
        assert "limit" in props
        assert props["query"]["type"] == "string"
        assert props["limit"]["type"] == "integer"
        assert props["limit"]["default"] == 10
        assert search.parameters["required"] == ["query"]

    def test_bool_param(self):
        @tool
        def toggle(enabled: bool) -> str:
            """Toggle feature."""
            return ""

        assert toggle.parameters["properties"]["enabled"]["type"] == "boolean"

    def test_float_param(self):
        @tool
        def set_temp(value: float = 0.7) -> str:
            """Set temperature."""
            return ""

        props = set_temp.parameters["properties"]
        assert props["value"]["type"] == "number"
        assert props["value"]["default"] == 0.7
        assert "required" not in set_temp.parameters  # all params have defaults

    def test_docstring_param_descriptions(self):
        @tool
        def fetch(url: str, timeout: int = 30) -> str:
            """Fetch a URL.

            Args:
                url: The URL to fetch
                timeout: Request timeout in seconds
            """
            return ""

        props = fetch.parameters["properties"]
        assert props["url"]["description"] == "The URL to fetch"
        assert props["timeout"]["description"] == "Request timeout in seconds"

    def test_no_docstring(self):
        @tool
        def mystery(x: str) -> str:
            return x

        assert mystery.description == "mystery"

    def test_function_is_preserved(self):
        def do_thing(a: str) -> str:
            """Do a thing."""
            return a.upper()

        td = tool(do_thing)
        assert td.function is do_thing
        assert td.function("hello") == "HELLO"

    def test_unknown_type_defaults_to_string(self):
        @tool
        def weird(data: list) -> str:
            """Handle weird types."""
            return ""

        assert weird.parameters["properties"]["data"]["type"] == "string"

    def test_self_and_cls_skipped(self):
        """Ensure self/cls params are excluded from schema."""

        @tool
        def method(self, query: str) -> str:
            """A method-like function."""
            return query

        assert "self" not in method.parameters["properties"]
        assert "query" in method.parameters["properties"]
