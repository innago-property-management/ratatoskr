"""Tests for gRPC reflection module — URL parsing, schema building, service extraction."""

from unittest.mock import MagicMock

from api_agent.grpc.reflection import (
    MethodInfo,
    ServiceInfo,
    build_schema_text,
    parse_grpc_target,
)


class TestParseGrpcTarget:
    """URL parsing for grpc:// and grpcs:// schemes."""

    def test_plaintext_with_port(self):
        target, tls = parse_grpc_target("grpc://localhost:50051")
        assert target == "localhost:50051"
        assert tls is False

    def test_tls_with_port(self):
        target, tls = parse_grpc_target("grpcs://api.example.com:8443")
        assert target == "api.example.com:8443"
        assert tls is True

    def test_tls_default_port(self):
        """grpcs:// without port defaults to 443."""
        target, tls = parse_grpc_target("grpcs://api.example.com")
        assert target == "api.example.com:443"
        assert tls is True

    def test_plaintext_no_port(self):
        """grpc:// without port returns host only (gRPC resolves)."""
        target, tls = parse_grpc_target("grpc://service.internal")
        assert target == "service.internal"
        assert tls is False

    def test_empty_host(self):
        target, tls = parse_grpc_target("grpc://")
        assert target == ""
        assert tls is False

    def test_hostname_with_subdomain(self):
        target, tls = parse_grpc_target("grpc://payments.prod.internal:50051")
        assert target == "payments.prod.internal:50051"
        assert tls is False


class TestBuildSchemaText:
    """Schema text generation for LLM context."""

    def _make_mock_pool(self, message_types: dict[str, list[tuple[str, int]]]):
        """Create a mock DescriptorPool that returns mock message descriptors.

        Args:
            message_types: {type_name: [(field_name, field_type), ...]}
                           field_type values from google.protobuf.descriptor.FieldDescriptor.TYPE_*
        """
        from google.protobuf.descriptor import FieldDescriptor

        pool = MagicMock()

        def find_message(name):
            if name not in message_types:
                raise KeyError(name)
            msg = MagicMock()
            msg.full_name = name
            fields = []
            for fname, ftype in message_types[name]:
                f = MagicMock()
                f.name = fname
                f.type = ftype
                f.label = FieldDescriptor.LABEL_OPTIONAL
                if ftype == FieldDescriptor.TYPE_MESSAGE:
                    f.message_type = MagicMock()
                    f.message_type.full_name = "google.protobuf.Timestamp"
                elif ftype == FieldDescriptor.TYPE_ENUM:
                    f.enum_type = MagicMock()
                    f.enum_type.full_name = "example.Status"
                fields.append(f)
            msg.fields = fields
            return msg

        pool.FindMessageTypeByName = find_message
        return pool

    def test_simple_unary_service(self):
        """Single service with one unary method."""
        from google.protobuf.descriptor import FieldDescriptor

        services = [
            ServiceInfo(
                full_name="helloworld.Greeter",
                methods=[
                    MethodInfo(
                        name="SayHello",
                        full_method_path="/helloworld.Greeter/SayHello",
                        input_type="helloworld.HelloRequest",
                        output_type="helloworld.HelloReply",
                    )
                ],
            )
        ]
        pool = self._make_mock_pool(
            {
                "helloworld.HelloRequest": [
                    ("name", FieldDescriptor.TYPE_STRING),
                ],
                "helloworld.HelloReply": [
                    ("message", FieldDescriptor.TYPE_STRING),
                ],
            }
        )

        text = build_schema_text(services, pool)

        assert "<services>" in text
        assert "helloworld.Greeter" in text
        assert "SayHello(helloworld.HelloRequest) -> helloworld.HelloReply" in text
        assert "<message_types>" in text
        assert "helloworld.HelloRequest {" in text
        assert "  string name" in text
        assert "helloworld.HelloReply {" in text
        assert "  string message" in text

    def test_streaming_method_tagged(self):
        """Streaming methods get [unsupported-v1] tags."""
        services = [
            ServiceInfo(
                full_name="test.Svc",
                methods=[
                    MethodInfo(
                        name="StreamData",
                        full_method_path="/test.Svc/StreamData",
                        input_type="test.Req",
                        output_type="test.Resp",
                        server_streaming=True,
                    )
                ],
            )
        ]
        pool = self._make_mock_pool(
            {
                "test.Req": [],
                "test.Resp": [],
            }
        )

        text = build_schema_text(services, pool)

        assert "server-streaming" in text
        assert "unsupported-v1" in text

    def test_bidi_streaming_tagged(self):
        """Bidi streaming methods get both tags."""
        services = [
            ServiceInfo(
                full_name="test.Svc",
                methods=[
                    MethodInfo(
                        name="BidiChat",
                        full_method_path="/test.Svc/BidiChat",
                        input_type="test.Req",
                        output_type="test.Resp",
                        client_streaming=True,
                        server_streaming=True,
                    )
                ],
            )
        ]
        pool = self._make_mock_pool({"test.Req": [], "test.Resp": []})

        text = build_schema_text(services, pool)

        assert "client-streaming" in text
        assert "server-streaming" in text
        assert "unsupported-v1" in text

    def test_multiple_services(self):
        """Multiple services appear in output."""
        services = [
            ServiceInfo(
                full_name="svc.A",
                methods=[
                    MethodInfo(
                        name="Do",
                        full_method_path="/svc.A/Do",
                        input_type="svc.AReq",
                        output_type="svc.AResp",
                    )
                ],
            ),
            ServiceInfo(
                full_name="svc.B",
                methods=[
                    MethodInfo(
                        name="Run",
                        full_method_path="/svc.B/Run",
                        input_type="svc.BReq",
                        output_type="svc.BResp",
                    )
                ],
            ),
        ]
        pool = self._make_mock_pool(
            {
                "svc.AReq": [],
                "svc.AResp": [],
                "svc.BReq": [],
                "svc.BResp": [],
            }
        )

        text = build_schema_text(services, pool)

        assert "svc.A" in text
        assert "svc.B" in text
        assert "Do(svc.AReq)" in text
        assert "Run(svc.BReq)" in text

    def test_enum_and_message_field_types(self):
        """Fields with enum and message types display type names."""
        from google.protobuf.descriptor import FieldDescriptor

        services = [
            ServiceInfo(
                full_name="test.Svc",
                methods=[
                    MethodInfo(
                        name="Get",
                        full_method_path="/test.Svc/Get",
                        input_type="test.GetReq",
                        output_type="test.GetResp",
                    )
                ],
            )
        ]
        pool = self._make_mock_pool(
            {
                "test.GetReq": [
                    ("status", FieldDescriptor.TYPE_ENUM),
                ],
                "test.GetResp": [
                    ("created_at", FieldDescriptor.TYPE_MESSAGE),
                ],
            }
        )

        text = build_schema_text(services, pool)

        assert "example.Status status" in text
        assert "google.protobuf.Timestamp created_at" in text

    def test_repeated_field(self):
        """Repeated fields show 'repeated' prefix."""
        from google.protobuf.descriptor import FieldDescriptor

        services = [
            ServiceInfo(
                full_name="test.Svc",
                methods=[
                    MethodInfo(
                        name="List",
                        full_method_path="/test.Svc/List",
                        input_type="test.ListReq",
                        output_type="test.ListResp",
                    )
                ],
            )
        ]

        pool = MagicMock()

        def find_message(name):
            msg = MagicMock()
            msg.full_name = name
            if name == "test.ListResp":
                f = MagicMock()
                f.name = "items"
                f.type = FieldDescriptor.TYPE_STRING
                f.label = FieldDescriptor.LABEL_REPEATED
                msg.fields = [f]
            else:
                msg.fields = []
            return msg

        pool.FindMessageTypeByName = find_message

        text = build_schema_text(services, pool)

        assert "repeated string items" in text

    def test_empty_services(self):
        """Empty service list produces minimal output."""
        text = build_schema_text([], MagicMock())
        assert "<services>" in text
        assert "<message_types>" not in text


class TestExtractServices:
    """Test _extract_services with mock DescriptorPool."""

    def test_skips_reflection_service(self):
        from api_agent.grpc.reflection import _extract_services

        pool = MagicMock()
        # Should not attempt to find the reflection service
        services = _extract_services(
            ["myapp.Users", "grpc.reflection.v1alpha.ServerReflection"], pool
        )
        # Only myapp.Users should be processed
        assert len(services) == 1
        assert services[0].full_name == "myapp.Users"

    def test_handles_missing_service_gracefully(self):
        from api_agent.grpc.reflection import _extract_services

        pool = MagicMock()
        pool.FindServiceByName.side_effect = KeyError("not found")

        services = _extract_services(["missing.Service"], pool)
        assert services == []
