"""TDD tests for generic spec reader, writer, and schema store."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import yaml

from api_agent.schema.spec_cache import SchemaStore, is_local_path
from api_agent.schema.spec_io import SpecReader, SpecWriter

# ---------------------------------------------------------------------------
# is_local_path
# ---------------------------------------------------------------------------


class TestIsLocalPath:
    """is_local_path correctly classifies file paths vs network targets."""

    def test_file_uri(self) -> None:
        assert is_local_path("file:///opt/spec.json") == (True, "/opt/spec.json")

    def test_absolute_path(self) -> None:
        assert is_local_path("/opt/spec.json") == (True, "/opt/spec.json")

    def test_relative_path(self) -> None:
        assert is_local_path("./spec.json") == (True, "./spec.json")

    def test_http_url(self) -> None:
        is_local, _ = is_local_path("http://api.example.com/spec")
        assert not is_local

    def test_https_url(self) -> None:
        is_local, _ = is_local_path("https://api.example.com/spec")
        assert not is_local

    def test_grpc_url(self) -> None:
        is_local, _ = is_local_path("grpc://localhost:50051")
        assert not is_local

    def test_bare_host_port_not_local(self) -> None:
        """host:port like 'localhost:50051' should NOT be classified as local."""
        is_local, _ = is_local_path("localhost:50051")
        assert not is_local

    def test_bare_ip_port_not_local(self) -> None:
        is_local, _ = is_local_path("10.0.0.1:8080")
        assert not is_local

    def test_bare_host_port_path_not_local(self) -> None:
        """host:port/path like 'localhost:8080/api' should NOT be classified as local."""
        is_local, _ = is_local_path("localhost:8080/api")
        assert not is_local


# ---------------------------------------------------------------------------
# SpecReader
# ---------------------------------------------------------------------------


class TestSpecReader:
    """SpecReader reads bytes from file, returns parsed content."""

    def test_reads_json_file(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0", "paths": {"/users": {}}}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))

        result = SpecReader.read_file(str(path))
        assert result == spec

    def test_reads_yaml_file(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0", "paths": {"/users": {}}}
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.dump(spec))

        result = SpecReader.read_file(str(path))
        assert result == spec

    def test_reads_plain_text_file(self, tmp_path) -> None:
        """For SDL/proto files — returns raw string, not parsed."""
        content = "type Query { users: [User] }"
        path = tmp_path / "schema.graphql"
        path.write_text(content)

        result = SpecReader.read_file(str(path))
        assert result == content

    def test_missing_file_returns_none(self, tmp_path) -> None:
        result = SpecReader.read_file(str(tmp_path / "nope.json"))
        assert result is None

    def test_corrupt_json_falls_through_to_yaml(self, tmp_path) -> None:
        """Content starting with { that isn't JSON falls through to YAML/text."""
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")

        result = SpecReader.read_file(str(path))
        # Not valid JSON or YAML dict — returned as plain text string
        assert isinstance(result, str)
        assert "not valid json" in result

    def test_yaml_flow_mapping_starting_with_brace(self, tmp_path) -> None:
        """YAML flow mappings like {'key': 'value'} start with { but are not JSON.

        SpecReader must fall through from JSON to YAML parser.
        """
        content = "{'openapi': '3.0.0', 'info': {'title': 'Test'}}"
        path = tmp_path / "flow.yaml"
        path.write_text(content)

        result = SpecReader.read_file(str(path))
        assert isinstance(result, dict)
        assert result["openapi"] == "3.0.0"

    def test_empty_file_returns_none(self, tmp_path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("")

        result = SpecReader.read_file(str(path))
        assert result is None

    def test_detects_json_by_content_not_extension(self, tmp_path) -> None:
        """A .yaml file containing JSON should parse as JSON."""
        spec = {"openapi": "3.0.0"}
        path = tmp_path / "spec.yaml"
        path.write_text(json.dumps(spec))

        result = SpecReader.read_file(str(path))
        assert result == spec


# ---------------------------------------------------------------------------
# SpecWriter
# ---------------------------------------------------------------------------


class TestSpecWriter:
    """SpecWriter writes parsed spec to a file."""

    def test_writes_dict_as_json(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0", "paths": {"/users": {}}}
        path = tmp_path / "out.json"

        SpecWriter.write_file(str(path), spec)

        loaded = json.loads(path.read_text())
        assert loaded == spec

    def test_writes_string_as_text(self, tmp_path) -> None:
        content = "type Query { users: [User] }"
        path = tmp_path / "out.graphql"

        SpecWriter.write_file(str(path), content)

        assert path.read_text() == content

    def test_creates_parent_directories(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0"}
        path = tmp_path / "nested" / "deep" / "spec.json"

        SpecWriter.write_file(str(path), spec)

        assert path.exists()
        assert json.loads(path.read_text()) == spec

    def test_overwrites_existing_file(self, tmp_path) -> None:
        path = tmp_path / "spec.json"
        path.write_text('{"old": true}')

        SpecWriter.write_file(str(path), {"new": True})

        assert json.loads(path.read_text()) == {"new": True}

    def test_write_failure_raises(self, monkeypatch, tmp_path) -> None:
        """Writing failures raise an OSError (OS-agnostic)."""

        def raising_open(*args, **kwargs):
            raise OSError("simulated write failure")

        monkeypatch.setattr("builtins.open", raising_open)

        with pytest.raises(OSError):
            SpecWriter.write_file(str(tmp_path / "spec.json"), {"a": 1})


# ---------------------------------------------------------------------------
# SchemaStore
# ---------------------------------------------------------------------------


class TestSchemaStoreFileSource:
    """SchemaStore.load() with file paths — reads from disk, no fetch."""

    @pytest.mark.asyncio
    async def test_loads_from_file_path(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0", "info": {"title": "Test"}}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))

        store = SchemaStore()
        result = await store.load(str(path), fetcher=AsyncMock())
        assert result == spec

    @pytest.mark.asyncio
    async def test_loads_from_file_uri(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0"}
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.dump(spec))

        store = SchemaStore()
        result = await store.load(f"file://{path}", fetcher=AsyncMock())
        assert result == spec

    @pytest.mark.asyncio
    async def test_file_source_never_calls_fetcher(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0"}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))

        fetcher = AsyncMock()
        store = SchemaStore()
        await store.load(str(path), fetcher=fetcher)
        fetcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_file_returns_none(self, tmp_path) -> None:
        store = SchemaStore()
        result = await store.load(str(tmp_path / "nope.json"), fetcher=AsyncMock())
        assert result is None


class TestSchemaStoreHttpSource:
    """SchemaStore.load() with HTTP URLs — fetches once, saves to disk."""

    @pytest.mark.asyncio
    async def test_fetches_and_saves_to_cache(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0", "info": {"title": "Fetched"}}
        fetcher = AsyncMock(return_value=spec)
        store = SchemaStore(cache_dir=str(tmp_path))

        result = await store.load("http://example.com/spec.json", fetcher=fetcher)

        assert result == spec
        fetcher.assert_awaited_once()
        # Verify saved to disk
        cached_files = list(tmp_path.iterdir())
        assert len(cached_files) == 1
        assert json.loads(cached_files[0].read_text()) == spec

    @pytest.mark.asyncio
    async def test_reads_from_disk_cache_instead_of_fetching(self, tmp_path) -> None:
        """Second startup reads from disk, no fetch needed."""
        spec = {"openapi": "3.0.0", "info": {"title": "Cached"}}

        # First startup — fetches and caches
        fetcher1 = AsyncMock(return_value=spec)
        store1 = SchemaStore(cache_dir=str(tmp_path))
        await store1.load("http://example.com/spec.json", fetcher=fetcher1)
        fetcher1.assert_awaited_once()

        # Second startup — reads from disk cache
        fetcher2 = AsyncMock()
        store2 = SchemaStore(cache_dir=str(tmp_path))
        result = await store2.load("http://example.com/spec.json", fetcher=fetcher2)

        assert result == spec
        fetcher2.assert_not_awaited()  # disk cache hit

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none(self) -> None:
        fetcher = AsyncMock(side_effect=RuntimeError("network down"))
        store = SchemaStore()
        result = await store.load("http://example.com/spec.json", fetcher=fetcher)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_cache_dir_still_holds_in_memory(self) -> None:
        spec = {"openapi": "3.0.0"}
        fetcher = AsyncMock(return_value=spec)
        store = SchemaStore(cache_dir=None)

        result = await store.load("http://example.com/spec.json", fetcher=fetcher)
        assert result == spec
        assert store.get("http://example.com/spec.json") == spec


class TestSchemaStoreGet:
    """SchemaStore.get() — runtime read from pre-loaded memory."""

    @pytest.mark.asyncio
    async def test_get_returns_loaded_schema(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0"}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))

        store = SchemaStore()
        await store.load(str(path), fetcher=AsyncMock())

        assert store.get(str(path)) == spec

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown(self) -> None:
        store = SchemaStore()
        assert store.get("http://never-loaded.com/spec") is None

    @pytest.mark.asyncio
    async def test_is_loaded(self, tmp_path) -> None:
        spec = {"openapi": "3.0.0"}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))

        store = SchemaStore()
        assert not store.is_loaded(str(path))
        await store.load(str(path), fetcher=AsyncMock())
        assert store.is_loaded(str(path))

    @pytest.mark.asyncio
    async def test_empty_source_returns_none(self) -> None:
        store = SchemaStore()
        result = await store.load("", fetcher=AsyncMock())
        assert result is None
