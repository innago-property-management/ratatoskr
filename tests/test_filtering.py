"""Tests for endpoint allowlist filtering logic."""

from api_agent.filtering import (
    _collect_referenced_types,
    _extract_type_name,
    filter_graphql_schema,
    filter_grpc_services,
    filter_openapi_spec,
    is_endpoint_allowed,
    matches_any_pattern,
    parse_config_allowlist,
)
from api_agent.grpc.reflection import MethodInfo, ServiceInfo

# ---------------------------------------------------------------------------
# parse_config_allowlist
# ---------------------------------------------------------------------------


class TestParseConfigAllowlist:
    """Parse CSV config string into tuple or None."""

    def test_empty_string_returns_none(self):
        assert parse_config_allowlist("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_config_allowlist("   ") is None

    def test_single_pattern(self):
        assert parse_config_allowlist("GET /users/*") == ("GET /users/*",)

    def test_multiple_patterns(self):
        result = parse_config_allowlist("GET /users/*,POST /orders")
        assert result == ("GET /users/*", "POST /orders")

    def test_strips_whitespace(self):
        result = parse_config_allowlist(" GET /users/* , POST /orders ")
        assert result == ("GET /users/*", "POST /orders")

    def test_ignores_empty_segments(self):
        result = parse_config_allowlist("GET /users/*,,POST /orders,")
        assert result == ("GET /users/*", "POST /orders")


# ---------------------------------------------------------------------------
# matches_any_pattern
# ---------------------------------------------------------------------------


class TestMatchesAnyPattern:
    """fnmatch-based pattern matching."""

    def test_none_patterns_allows_all(self):
        assert matches_any_pattern("anything", None) is True

    def test_empty_tuple_blocks_all(self):
        assert matches_any_pattern("anything", ()) is False

    def test_exact_match(self):
        assert matches_any_pattern("GET /users", ("GET /users",)) is True

    def test_glob_wildcard(self):
        assert matches_any_pattern("GET /users/123", ("GET /users/*",)) is True

    def test_glob_star_all(self):
        assert matches_any_pattern("GET /anything", ("*",)) is True

    def test_no_match(self):
        assert matches_any_pattern("POST /users", ("GET /users/*",)) is False

    def test_multiple_patterns_any_matches(self):
        patterns = ("GET /users/*", "GET /orders/*")
        assert matches_any_pattern("GET /users/1", patterns) is True
        assert matches_any_pattern("GET /orders/1", patterns) is True
        assert matches_any_pattern("GET /products/1", patterns) is False

    def test_case_sensitive(self):
        """fnmatch is case-sensitive by default."""
        assert matches_any_pattern("GET /users", ("get /users",)) is False

    def test_question_mark_wildcard(self):
        assert matches_any_pattern("GET /users/a", ("GET /users/?",)) is True
        assert matches_any_pattern("GET /users/ab", ("GET /users/?",)) is False


# ---------------------------------------------------------------------------
# is_endpoint_allowed (intersection logic)
# ---------------------------------------------------------------------------


class TestIsEndpointAllowed:
    """Config + header intersection semantics."""

    def test_both_none_allows_all(self):
        """Neither config nor header set = allow everything."""
        assert is_endpoint_allowed("GET /users", None, None) is True

    def test_config_only_matches(self):
        assert is_endpoint_allowed("GET /users", ("GET /users",), None) is True

    def test_config_only_no_match(self):
        assert is_endpoint_allowed("POST /users", ("GET /users",), None) is False

    def test_header_only_matches(self):
        assert is_endpoint_allowed("GET /users", None, ("GET /users",)) is True

    def test_header_only_no_match(self):
        assert is_endpoint_allowed("POST /users", None, ("GET /users",)) is False

    def test_both_set_intersection_both_match(self):
        """Both must match for intersection."""
        config = ("GET /users/*", "GET /orders/*")
        header = ("GET /users/*",)
        assert is_endpoint_allowed("GET /users/1", config, header) is True

    def test_both_set_intersection_header_narrows(self):
        """Header narrows config — orders in config but not header."""
        config = ("GET /users/*", "GET /orders/*")
        header = ("GET /users/*",)
        assert is_endpoint_allowed("GET /orders/1", config, header) is False

    def test_both_set_intersection_disjoint(self):
        """Disjoint config and header = nothing allowed."""
        config = ("GET /users/*",)
        header = ("GET /orders/*",)
        assert is_endpoint_allowed("GET /users/1", config, header) is False
        assert is_endpoint_allowed("GET /orders/1", config, header) is False

    def test_header_cannot_widen_config(self):
        """Header allows broader pattern but config restricts."""
        config = ("GET /users/*",)
        header = ("*",)  # wildcard header
        assert is_endpoint_allowed("GET /users/1", config, header) is True
        assert is_endpoint_allowed("GET /orders/1", config, header) is False


# ---------------------------------------------------------------------------
# filter_openapi_spec (REST)
# ---------------------------------------------------------------------------

_SAMPLE_SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/users": {
            "get": {"summary": "List users", "responses": {}},
            "post": {"summary": "Create user", "responses": {}},
        },
        "/users/{id}": {
            "get": {"summary": "Get user", "responses": {}},
            "delete": {"summary": "Delete user", "responses": {}},
        },
        "/orders": {
            "get": {"summary": "List orders", "responses": {}},
        },
    },
    "components": {"schemas": {"User": {"type": "object"}, "Order": {"type": "object"}}},
}


class TestFilterOpenapiSpec:
    """Filter OpenAPI spec paths by allowlist."""

    def test_none_patterns_returns_unchanged(self):
        """No constraints = full spec."""
        result = filter_openapi_spec(_SAMPLE_SPEC, None, None)
        assert set(result["paths"].keys()) == {"/users", "/users/{id}", "/orders"}

    def test_filter_by_method_and_path(self):
        """Only GET /users/* allowed."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /users*",), None)
        assert "/users" in result["paths"]
        assert "get" in result["paths"]["/users"]
        assert "post" not in result["paths"]["/users"]
        assert "/users/{id}" in result["paths"]
        assert "/orders" not in result["paths"]

    def test_multiple_patterns(self):
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /users*", "GET /orders"), None)
        assert "/users" in result["paths"]
        assert "/orders" in result["paths"]

    def test_wildcard_method(self):
        """'* /users' matches any method on /users."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("* /users",), None)
        assert "get" in result["paths"]["/users"]
        assert "post" in result["paths"]["/users"]
        assert "/orders" not in result["paths"]

    def test_all_filtered_empty_paths(self):
        """Nothing matches = empty paths."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /nonexistent",), None)
        assert result["paths"] == {}

    def test_components_preserved(self):
        """Schemas/components always preserved regardless of path filtering."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /users",), None)
        assert result["components"]["schemas"]["User"] == {"type": "object"}
        assert result["components"]["schemas"]["Order"] == {"type": "object"}

    def test_intersection_narrows(self):
        """Config allows users+orders, header narrows to users only."""
        config = ("GET /users*", "GET /orders")
        header = ("GET /users*",)
        result = filter_openapi_spec(_SAMPLE_SPEC, config, header)
        assert "/users" in result["paths"]
        assert "/orders" not in result["paths"]

    def test_does_not_mutate_original(self):
        """Filtering returns a new dict, original is unchanged."""
        import copy

        original = copy.deepcopy(_SAMPLE_SPEC)
        filter_openapi_spec(_SAMPLE_SPEC, ("GET /users",), None)
        assert _SAMPLE_SPEC == original

    def test_path_level_params_preserved(self):
        """Path-level keys other than methods are preserved."""
        spec = {
            "paths": {
                "/users": {
                    "parameters": [{"name": "limit", "in": "query"}],
                    "get": {"summary": "List", "responses": {}},
                }
            }
        }
        result = filter_openapi_spec(spec, ("GET /users",), None)
        assert "parameters" in result["paths"]["/users"]

    def test_extension_keys_preserved(self):
        """x- extension keys on path items are preserved."""
        spec = {
            "paths": {
                "/users": {
                    "x-custom": "value",
                    "get": {"summary": "List", "responses": {}},
                }
            }
        }
        result = filter_openapi_spec(spec, ("GET /users",), None)
        assert result["paths"]["/users"].get("x-custom") == "value"


# ---------------------------------------------------------------------------
# filter_grpc_services (gRPC)
# ---------------------------------------------------------------------------


def _make_method(svc: str, name: str) -> MethodInfo:
    return MethodInfo(
        name=name,
        full_method_path=f"/{svc}/{name}",
        input_type=f"{svc}.{name}Request",
        output_type=f"{svc}.{name}Response",
    )


_SAMPLE_GRPC_SERVICES = [
    ServiceInfo(
        full_name="users.UserService",
        methods=[
            _make_method("users.UserService", "GetUser"),
            _make_method("users.UserService", "ListUsers"),
            _make_method("users.UserService", "CreateUser"),
        ],
    ),
    ServiceInfo(
        full_name="orders.OrderService",
        methods=[
            _make_method("orders.OrderService", "GetOrder"),
            _make_method("orders.OrderService", "ListOrders"),
        ],
    ),
]


class TestFilterGrpcServices:
    """Filter gRPC services/methods by allowlist."""

    def test_none_patterns_returns_all(self):
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, None, None)
        assert len(result) == 2
        assert len(result[0].methods) == 3

    def test_filter_by_exact_method(self):
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, ("users.UserService/GetUser",), None)
        assert len(result) == 1
        assert result[0].full_name == "users.UserService"
        assert len(result[0].methods) == 1
        assert result[0].methods[0].name == "GetUser"

    def test_filter_by_service_wildcard(self):
        """'service/*' matches all methods in a service."""
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, ("orders.OrderService/*",), None)
        assert len(result) == 1
        assert result[0].full_name == "orders.OrderService"
        assert len(result[0].methods) == 2

    def test_filter_by_partial_method_name(self):
        """'*/Get*' matches Get methods across all services."""
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, ("*/Get*",), None)
        assert len(result) == 2
        for svc in result:
            for m in svc.methods:
                assert m.name.startswith("Get")

    def test_all_methods_filtered_removes_service(self):
        """Service with zero matching methods is excluded."""
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, ("orders.OrderService/*",), None)
        svc_names = [s.full_name for s in result]
        assert "users.UserService" not in svc_names

    def test_empty_result(self):
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, ("nonexistent/*",), None)
        assert result == []

    def test_intersection_narrows(self):
        config = ("users.UserService/*", "orders.OrderService/*")
        header = ("users.UserService/*",)
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, config, header)
        assert len(result) == 1
        assert result[0].full_name == "users.UserService"

    def test_does_not_mutate_original(self):
        original_len = len(_SAMPLE_GRPC_SERVICES[0].methods)
        filter_grpc_services(_SAMPLE_GRPC_SERVICES, ("users.UserService/GetUser",), None)
        assert len(_SAMPLE_GRPC_SERVICES[0].methods) == original_len

    def test_multiple_patterns(self):
        patterns = ("users.UserService/GetUser", "orders.OrderService/GetOrder")
        result = filter_grpc_services(_SAMPLE_GRPC_SERVICES, patterns, None)
        assert len(result) == 2
        assert all(len(s.methods) == 1 for s in result)


# ---------------------------------------------------------------------------
# GraphQL: type name extraction + transitive closure + schema filtering
# ---------------------------------------------------------------------------


# Helper to build type refs in introspection format
def _scalar(name: str) -> dict:
    return {"kind": "SCALAR", "name": name, "ofType": None}


def _object_ref(name: str) -> dict:
    return {"kind": "OBJECT", "name": name, "ofType": None}


def _non_null(inner: dict) -> dict:
    return {"kind": "NON_NULL", "name": None, "ofType": inner}


def _list_of(inner: dict) -> dict:
    return {"kind": "LIST", "name": None, "ofType": inner}


def _field(name: str, type_ref: dict, args: list | None = None) -> dict:
    return {"name": name, "args": args or [], "type": type_ref, "description": None}


def _input_field(name: str, type_ref: dict) -> dict:
    return {"name": name, "type": type_ref}


# A schema with: Query.users -> [User], Query.posts -> [Post]
# User has field address -> Address
# Post has field category -> Category (enum)
# Address is a plain object with scalar fields
_GRAPHQL_SCHEMA = {
    "queryType": {
        "fields": [
            _field(
                "users",
                _non_null(_list_of(_object_ref("User"))),
                [
                    {"name": "limit", "type": _scalar("Int"), "defaultValue": None},
                ],
            ),
            _field("posts", _non_null(_list_of(_object_ref("Post")))),
            _field("internal", _object_ref("InternalData")),
        ]
    },
    "types": [
        {
            "name": "User",
            "kind": "OBJECT",
            "fields": [
                _field("id", _non_null(_scalar("ID"))),
                _field("name", _scalar("String")),
                _field("address", _object_ref("Address")),
            ],
            "interfaces": [],
            "enumValues": None,
            "inputFields": None,
            "possibleTypes": None,
        },
        {
            "name": "Address",
            "kind": "OBJECT",
            "fields": [
                _field("city", _scalar("String")),
                _field("country", _object_ref("Country")),
            ],
            "interfaces": [],
            "enumValues": None,
            "inputFields": None,
            "possibleTypes": None,
        },
        {
            "name": "Country",
            "kind": "OBJECT",
            "fields": [_field("code", _scalar("String")), _field("name", _scalar("String"))],
            "interfaces": [],
            "enumValues": None,
            "inputFields": None,
            "possibleTypes": None,
        },
        {
            "name": "Post",
            "kind": "OBJECT",
            "fields": [
                _field("id", _non_null(_scalar("ID"))),
                _field("title", _scalar("String")),
                _field("category", _object_ref("Category")),
            ],
            "interfaces": [],
            "enumValues": None,
            "inputFields": None,
            "possibleTypes": None,
        },
        {
            "name": "Category",
            "kind": "ENUM",
            "fields": None,
            "enumValues": [{"name": "NEWS"}, {"name": "TECH"}],
            "interfaces": None,
            "inputFields": None,
            "possibleTypes": None,
        },
        {
            "name": "InternalData",
            "kind": "OBJECT",
            "fields": [_field("secret", _scalar("String"))],
            "interfaces": [],
            "enumValues": None,
            "inputFields": None,
            "possibleTypes": None,
        },
        {
            "name": "Query",
            "kind": "OBJECT",
            "fields": [],
            "interfaces": [],
            "enumValues": None,
            "inputFields": None,
            "possibleTypes": None,
        },
    ],
}


class TestExtractTypeName:
    """Unwrap NON_NULL/LIST wrappers to get leaf type name."""

    def test_scalar(self):
        assert _extract_type_name(_scalar("String")) == "String"

    def test_object(self):
        assert _extract_type_name(_object_ref("User")) == "User"

    def test_non_null_wrapping(self):
        assert _extract_type_name(_non_null(_scalar("ID"))) == "ID"

    def test_list_wrapping(self):
        assert _extract_type_name(_list_of(_object_ref("User"))) == "User"

    def test_nested_wrapping(self):
        assert _extract_type_name(_non_null(_list_of(_non_null(_object_ref("User"))))) == "User"

    def test_none_returns_none(self):
        assert _extract_type_name(None) is None


class TestCollectReferencedTypes:
    """Transitive closure of type references."""

    def _build_types_map(self):
        types = _GRAPHQL_SCHEMA["types"]
        assert isinstance(types, list)
        return {t["name"]: t for t in types}

    def test_user_includes_address_and_country(self):
        types_map = self._build_types_map()
        collected: set[str] = set()
        _collect_referenced_types("User", types_map, collected)
        assert "User" in collected
        assert "Address" in collected
        assert "Country" in collected

    def test_user_does_not_include_post(self):
        types_map = self._build_types_map()
        collected: set[str] = set()
        _collect_referenced_types("User", types_map, collected)
        assert "Post" not in collected
        assert "Category" not in collected

    def test_post_includes_category_enum(self):
        types_map = self._build_types_map()
        collected: set[str] = set()
        _collect_referenced_types("Post", types_map, collected)
        assert "Post" in collected
        assert "Category" in collected

    def test_scalar_not_collected(self):
        types_map = self._build_types_map()
        collected: set[str] = set()
        _collect_referenced_types("User", types_map, collected)
        assert "String" not in collected
        assert "ID" not in collected
        assert "Int" not in collected

    def test_circular_reference_safe(self):
        """Circular type refs don't cause infinite recursion."""
        circular_types = {
            "A": {
                "name": "A",
                "kind": "OBJECT",
                "fields": [_field("b", _object_ref("B"))],
                "interfaces": [],
                "enumValues": None,
                "inputFields": None,
                "possibleTypes": None,
            },
            "B": {
                "name": "B",
                "kind": "OBJECT",
                "fields": [_field("a", _object_ref("A"))],
                "interfaces": [],
                "enumValues": None,
                "inputFields": None,
                "possibleTypes": None,
            },
        }
        collected: set[str] = set()
        _collect_referenced_types("A", circular_types, collected)
        assert collected == {"A", "B"}


class TestFilterGraphqlSchema:
    """Filter GraphQL introspection schema by query field allowlist."""

    def test_none_patterns_returns_unchanged(self):
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, None, None)
        assert len(result["queryType"]["fields"]) == 3

    def test_filter_single_query_field(self):
        """Allow only Query.users — should keep User, Address, Country."""
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, ("Query.users",), None)
        fields = result["queryType"]["fields"]
        assert len(fields) == 1
        assert fields[0]["name"] == "users"
        type_names = {t["name"] for t in result["types"]}
        assert "User" in type_names
        assert "Address" in type_names
        assert "Country" in type_names
        assert "Post" not in type_names
        assert "Category" not in type_names
        assert "InternalData" not in type_names

    def test_filter_with_wildcard(self):
        """'Query.user*' matches 'users' only (not 'posts')."""
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, ("Query.user*",), None)
        field_names = [f["name"] for f in result["queryType"]["fields"]]
        assert "users" in field_names
        assert "posts" not in field_names

    def test_multiple_allowed_fields(self):
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, ("Query.users", "Query.posts"), None)
        field_names = {f["name"] for f in result["queryType"]["fields"]}
        assert field_names == {"users", "posts"}
        type_names = {t["name"] for t in result["types"]}
        assert "User" in type_names
        assert "Post" in type_names
        assert "Category" in type_names

    def test_all_fields_filtered(self):
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, ("Query.nonexistent",), None)
        assert result["queryType"]["fields"] == []

    def test_intersection_narrows(self):
        config = ("Query.users", "Query.posts")
        header = ("Query.users",)
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, config, header)
        assert len(result["queryType"]["fields"]) == 1
        assert result["queryType"]["fields"][0]["name"] == "users"

    def test_query_type_always_in_types(self):
        """Query type is always preserved in types list."""
        result = filter_graphql_schema(_GRAPHQL_SCHEMA, ("Query.users",), None)
        type_names = {t["name"] for t in result["types"]}
        assert "Query" in type_names

    def test_does_not_mutate_original(self):
        import copy

        original = copy.deepcopy(_GRAPHQL_SCHEMA)
        filter_graphql_schema(_GRAPHQL_SCHEMA, ("Query.users",), None)
        assert _GRAPHQL_SCHEMA == original

    def test_args_type_refs_included(self):
        """Types referenced by field arguments are included in closure."""
        schema = {
            "queryType": {
                "fields": [
                    _field(
                        "search",
                        _object_ref("Result"),
                        [
                            {
                                "name": "input",
                                "type": _object_ref("SearchInput"),
                                "defaultValue": None,
                            },
                        ],
                    ),
                ]
            },
            "types": [
                {
                    "name": "Result",
                    "kind": "OBJECT",
                    "fields": [_field("id", _scalar("ID"))],
                    "interfaces": [],
                    "enumValues": None,
                    "inputFields": None,
                    "possibleTypes": None,
                },
                {
                    "name": "SearchInput",
                    "kind": "INPUT_OBJECT",
                    "fields": None,
                    "inputFields": [_input_field("query", _scalar("String"))],
                    "interfaces": None,
                    "enumValues": None,
                    "possibleTypes": None,
                },
                {
                    "name": "Query",
                    "kind": "OBJECT",
                    "fields": [],
                    "interfaces": [],
                    "enumValues": None,
                    "inputFields": None,
                    "possibleTypes": None,
                },
            ],
        }
        result = filter_graphql_schema(schema, ("Query.search",), None)
        type_names = {t["name"] for t in result["types"]}
        assert "SearchInput" in type_names
        assert "Result" in type_names
