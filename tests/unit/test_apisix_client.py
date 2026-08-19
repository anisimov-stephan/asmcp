# tests/unit/test_apisix_client.py
"""Unit tests for the APISIX Admin API client."""

import httpx
import pytest

from src.clients.apisix_client import (
    ApisixClient,
    ApisixError,
    ApisixNotFoundError,
    apisix_path_from_uri,
    route_id_from_uri,
)


@pytest.fixture
def mock_env(monkeypatch):
    # CONTRACT: Fixture->MockEnv->SetApisixEnvVars
    monkeypatch.setenv("APISIX_ADMIN_URL", "http://apisix.test:9180")
    monkeypatch.setenv("APISIX_ADMIN_KEY", "test-key")
    monkeypatch.setenv("EXTERNAL_HTTP_TIMEOUT_S", "30")


def make_client(handler) -> ApisixClient:
    # CONTRACT: Fixture->MakeClient->InjectMockTransport
    transport = httpx.MockTransport(handler)
    return ApisixClient(http_client=httpx.AsyncClient(transport=transport))


class TestRouteIdFromUri:
    """Test route id normalization."""

    def test_strips_leading_slash_and_replaces_slashes(self):
        # CONTRACT: TestRouteId->Normalize->ColonsKeepBraces
        assert route_id_from_uri("/users/{id}/orders") == "users:{id}:orders"

    def test_no_leading_slash(self):
        # CONTRACT: TestRouteId->NoLeadingSlash->Normalize
        assert route_id_from_uri("users") == "users"

    def test_root_uri(self):
        # CONTRACT: TestRouteId->RootUri->EmptyString
        assert route_id_from_uri("/") == ""


class TestApisixPathFromUri:
    """Test URI path variable conversion."""

    def test_converts_params_to_variables(self):
        # CONTRACT: TestApisixPath->Convert->ColonVariables
        assert apisix_path_from_uri("/users/{id}/orders") == "/users/:id/orders"

    def test_adds_missing_leading_slash(self):
        # CONTRACT: TestApisixPath->AddSlash->LeadingSlash
        assert apisix_path_from_uri("users/{id}") == "/users/:id"

    def test_plain_path_unchanged(self):
        # CONTRACT: TestApisixPath->Plain->Unchanged
        assert apisix_path_from_uri("/health") == "/health"


class TestRead:
    """Test GET operations."""

    @pytest.mark.asyncio
    async def test_read_route_unwraps_value_envelope(self, mock_env):
        # CONTRACT: TestRead->Envelope->ReturnValueObject
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/apisix/admin/routes/users:{id}"
            assert request.headers["X-API-KEY"] == "test-key"
            return httpx.Response(
                200, json={"key": "/apisix/routes/1", "value": {"name": "users:{id}"}}
            )

        client = make_client(handler)
        result = await client.read("route", "users:{id}")
        assert result == {"name": "users:{id}"}

    @pytest.mark.asyncio
    async def test_read_service_plain_object(self, mock_env):
        # CONTRACT: TestRead->PlainObject->ReturnAsIs
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/apisix/admin/services/svc-1"
            return httpx.Response(200, json={"id": "svc-1"})

        client = make_client(handler)
        result = await client.read("service", "svc-1")
        assert result == {"id": "svc-1"}

    @pytest.mark.asyncio
    async def test_read_upstream(self, mock_env):
        # CONTRACT: TestRead->Upstream->ReturnObject
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/apisix/admin/upstreams/up-1"
            return httpx.Response(200, json={"id": "up-1"})

        client = make_client(handler)
        result = await client.read("upstream", "up-1")
        assert result == {"id": "up-1"}

    @pytest.mark.asyncio
    async def test_read_not_found_raises(self, mock_env):
        # CONTRACT: TestRead->NotFound->RaiseNotFound
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error_msg": "not found"})

        client = make_client(handler)
        with pytest.raises(ApisixNotFoundError):
            await client.read("route", "missing")

    @pytest.mark.asyncio
    async def test_read_server_error_raises(self, mock_env):
        # CONTRACT: TestRead->ServerError->RaiseApisixError
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = make_client(handler)
        with pytest.raises(ApisixError, match="500"):
            await client.read("route", "x")

    @pytest.mark.asyncio
    async def test_read_unknown_kind_raises(self, mock_env):
        # CONTRACT: TestRead->UnknownKind->RaiseApisixError
        client = make_client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ApisixError, match="Unknown APISIX object kind"):
            await client.read("plugin", "x")

    @pytest.mark.asyncio
    async def test_missing_base_url_raises(self, monkeypatch):
        # CONTRACT: TestRead->NoBaseUrl->RaiseApisixError
        monkeypatch.delenv("APISIX_ADMIN_URL", raising=False)
        client = make_client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ApisixError, match="APISIX_ADMIN_URL"):
            await client.read("route", "x")


class TestWriteOperations:
    """Test PUT/DELETE operations."""

    @pytest.mark.asyncio
    async def test_put_route(self, mock_env):
        # CONTRACT: TestPut->Route->SendPayload
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"value": {"name": "r"}})

        client = make_client(handler)
        result = await client.put_route("r", {"name": "r"})
        assert captured["method"] == "PUT"
        assert captured["path"] == "/apisix/admin/routes/r"
        assert captured["body"] == {"name": "r"}
        assert result == {"name": "r"}

    @pytest.mark.asyncio
    async def test_put_route_error(self, mock_env):
        # CONTRACT: TestPut->Error->RaiseApisixError
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad route")

        client = make_client(handler)
        with pytest.raises(ApisixError, match="400"):
            await client.put_route("r", {})

    @pytest.mark.asyncio
    async def test_delete_route(self, mock_env):
        # CONTRACT: TestDelete->Route->SendDelete
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={"deleted": True})

        client = make_client(handler)
        await client.delete_route("r")
        assert captured["method"] == "DELETE"
        assert captured["path"] == "/apisix/admin/routes/r"
