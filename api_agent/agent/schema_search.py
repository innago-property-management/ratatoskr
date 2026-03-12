"""Shared schema search implementation for GraphQL and REST agents."""

import re
from contextvars import ContextVar
from typing import Callable

from ..config import settings
from ..llm.tools import tool as function_tool

MAX_PATTERN_LENGTH = 200


def create_search_schema_tool(raw_schema_var: ContextVar[str]):
    """Create a search_schema function_tool bound to a specific context var.

    Args:
        raw_schema_var: ContextVar holding the raw schema JSON string

    Returns:
        A FunctionTool for search_schema
    """
    impl = create_search_schema_impl(raw_schema_var)

    @function_tool
    def search_schema(
        pattern: str,
        context: int = 10,
        before: int = 0,
        after: int = 0,
        offset: int = 0,
    ) -> str:
        """Grep-like search on schema. Output: "line_num:match" or "line_num-context".

        Args:
            pattern: Regex pattern (case-insensitive)
            context: Lines around each match (default 10)
            before: Lines before match (overrides context)
            after: Lines after match (overrides context)
            offset: Number of matches to skip (for pagination)
        """
        return impl(
            pattern,
            before=before,
            after=after,
            context=context,
            offset=offset,
        )

    return search_schema


def create_search_schema_impl(
    raw_schema_var: ContextVar[str],
) -> Callable[..., str]:
    """Create a search_schema_impl function bound to a specific context var.

    Args:
        raw_schema_var: ContextVar holding the raw schema JSON string

    Returns:
        A search implementation function
    """

    def _search_schema_impl(
        pattern: str,
        before: int = 0,
        after: int = 0,
        context: int = 10,
        offset: int = 0,
        max_chars: int | None = None,
    ) -> str:
        """Grep-like search on raw schema JSON.

        Args:
            pattern: Regex pattern (case-insensitive)
            before: Lines before match (-B)
            after: Lines after match (-A)
            context: Lines around match (-C), overridden by before/after
            offset: Number of matches to skip before showing results
            max_chars: Character budget for the entire response

        Returns:
            Grep-like output: "line_num:content" or "line_num-context" for context
        """
        try:
            schema = raw_schema_var.get()
        except LookupError:
            return "error: schema not loaded"

        if not schema:
            return "error: schema empty"

        if offset < 0:
            return "error: offset must be >= 0"

        char_limit = max_chars or settings.MAX_TOOL_RESPONSE_CHARS
        if char_limit <= 0:
            return "error: max_chars must be > 0"

        if len(pattern) > MAX_PATTERN_LENGTH:
            return (
                f"error: pattern too long ({len(pattern)} chars, "
                f"max {MAX_PATTERN_LENGTH})"
            )

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"error: invalid regex - {e}"

        lines = schema.split("\n")
        matched_indices = [idx for idx, line in enumerate(lines) if regex.search(line)]

        if not matched_indices:
            return "(no matches)"

        total_matches = len(matched_indices)

        if offset >= total_matches:
            return (
                f"({total_matches} matches) "
                f"offset {offset} is beyond available results. Try a smaller offset."
            )

        # Resolve context: before/after override context
        b = before if before > 0 else context
        a = after if after > 0 else context

        # Build blocks for all matches after offset
        blocks: list[str] = []
        for i in matched_indices[offset:]:
            start = max(0, i - b)
            end = min(len(lines), i + a + 1)
            block_lines: list[str] = []
            for j in range(start, end):
                ln = j + 1  # 1-indexed
                sep = ":" if j == i else "-"
                block_lines.append(f"{ln}{sep}{lines[j]}")
            blocks.append("\n".join(block_lines))

        def assemble(selected: list[str]) -> str:
            shown = len(selected)
            has_more = (offset + shown) < total_matches
            header_parts = [f"{total_matches} matches"]
            if offset:
                header_parts.append(f"starting at match {min(offset + 1, total_matches)}")
            header_parts.append(f"showing {shown}")
            if has_more:
                header_parts.append("truncated")
            header = "(" + ", ".join(header_parts) + ")"

            sections = [header]
            if selected:
                sections.append("\n--\n".join(selected))

            if has_more:
                next_offset = min(total_matches, offset + max(shown, 1))
                sections.append(
                    f"[TRUNCATED at {char_limit} chars - rerun search_schema with offset={next_offset}]"
                )

            return "\n".join(sections)

        selected_blocks = blocks.copy()
        output = assemble(selected_blocks)

        while len(output) > char_limit and selected_blocks:
            selected_blocks.pop()
            output = assemble(selected_blocks)

        if not selected_blocks:
            return (
                f"({total_matches} matches) Unable to show any results within "
                f"max_chars={char_limit}. Reduce context or increase max_chars."
            )

        if len(output) > char_limit:
            return (
                f"({total_matches} matches) Output exceeds max_chars={char_limit}. "
                "Reduce context/before/after or increase max_chars."
            )

        return output

    return _search_schema_impl
