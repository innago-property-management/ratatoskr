"""Tests for ReDoS guard on search_schema pattern length (M3)."""

from contextvars import ContextVar

from api_agent.agent.schema_search import MAX_PATTERN_LENGTH, create_search_schema_impl

SAMPLE_SCHEMA = "type Query {\n  hello: String\n  world: Int\n}\n"

_schema_var: ContextVar[str] = ContextVar("_test_schema")


def _make_impl():
    token = _schema_var.set(SAMPLE_SCHEMA)
    return create_search_schema_impl(_schema_var), token


def test_pattern_at_max_length_accepted():
    impl, _ = _make_impl()
    pattern = "a" * MAX_PATTERN_LENGTH
    result = impl(pattern)
    assert "error: pattern too long" not in result


def test_pattern_one_over_max_rejected():
    impl, _ = _make_impl()
    pattern = "a" * (MAX_PATTERN_LENGTH + 1)
    result = impl(pattern)
    assert "error: pattern too long" in result
    assert f"{MAX_PATTERN_LENGTH + 1} chars" in result
    assert f"max {MAX_PATTERN_LENGTH}" in result


def test_pattern_very_long_rejected():
    impl, _ = _make_impl()
    pattern = "x" * 1000
    result = impl(pattern)
    assert "error: pattern too long" in result
    assert "1000 chars" in result
    assert f"max {MAX_PATTERN_LENGTH}" in result


def test_normal_short_pattern_works():
    impl, _ = _make_impl()
    result = impl("hello")
    assert "hello" in result
    assert "error" not in result


def test_error_message_includes_length_and_max():
    impl, _ = _make_impl()
    length = 350
    result = impl("z" * length)
    assert f"{length} chars" in result
    assert f"max {MAX_PATTERN_LENGTH}" in result
