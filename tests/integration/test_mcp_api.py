# tests/integration/test_mcp_api.py
"""MCP-level integration tests exercising the tools over the mounted app."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.sync_service import SyncError

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

CREATE_ARGS = {
    "params": {
        "uri": "/users/{id}",
        "anchor": "https://docs#users",
        "method": "GET",
        "service_id": "svc-1",
        "function_code": "def get_user(user_id: int) -> dict:\n    return {}\n",
        "function_path": "mpsdk/users.py",
    }
}


def parse_sse(response) -> dict:
    # CONTRACT: Helper->ParseSse->ExtractJsonData
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"No data line in SSE response: {response.text}")


@pytest.fixture
def mock_keycloak_decode():
    # CONTRACT: Fixture->MockKeycloak->PatchTokenDecoding
    claims = {"sub": "test-agent"}
    with (
        patch(
            "micro.shared.api.middleware.decode_keycloak_token",
            new=AsyncMock(return_value=claims),
        ),
        patch(
            "micro.shared.api.auth.decode_keycloak_token",
            new=AsyncMock(return_value=claims),
        ),
    ):
        yield claims


@pytest.fixture
def mock_lifespan_infra():
    # CONTRACT: Fixture->MockLifespanInfra->PatchDbAndTelemetry
    with (
        patch("src.main.ensure_micro_tasks_schema", new_callable=AsyncMock),
        patch("src.main.telemetry_service"),
    ):
        yield


@pytest.fixture
def client(mock_keycloak_decode, mock_lifespan_infra):
    # CONTRACT: Fixture->CreateClient->RunLifespanWithMcp
    with TestClient(app) as test_client:
        yield test_client


def mcp_session(client: TestClient) -> dict:
    # CONTRACT: Helper->McpSession->InitializeAndReturnHeaders
    headers = {**MCP_HEADERS, "Authorization": "Bearer test-token"}
    init_response = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.0.1"},
            },
        },
    )
    assert init_response.status_code == 200
    session_id = init_response.headers["mcp-session-id"]
    protocol_version = parse_sse(init_response)["result"]["protocolVersion"]
    headers["mcp-session-id"] = session_id
    headers["mcp-protocol-version"] = protocol_version
    notify_response = client.post(
        "/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notify_response.status_code == 202
    return headers


class TestMcpAuth:
    """Test that /mcp is protected by the Keycloak JWT middleware."""

    def test_mcp_requires_auth(self, client):
        # CONTRACT: TestMcpAuth->NoToken->Return401
        response = client.post(
            "/mcp/",
            headers=MCP_HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert response.status_code == 401


class TestMcpTools:
    """Test MCP tools over the mounted streamable HTTP app."""

    def test_tools_list(self, client):
        # CONTRACT: TestMcpTools->ListTools->SevenToolsExposed
        headers = mcp_session(client)
        response = client.post(
            "/mcp/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200
        tools = parse_sse(response)["result"]["tools"]
        assert {tool["name"] for tool in tools} == {
            "apisix_read",
            "mpsdk_read",
            "create_route",
            "update_route",
            "deprecate_route",
            "delete_route",
            "report_issue",
        }

    def test_create_route_happy_path(self, client):
        # CONTRACT: TestMcpTools->CreateRoute->ReturnStructuredResult
        headers = mcp_session(client)
        with (
            patch("src.mcp_server.sync_service") as mock_service,
            patch("src.mcp_server.log_tool_call", new=AsyncMock()),
        ):
            mock_service.create_route = AsyncMock(
                return_value={"status": "ok", "apisix": "applied", "gitlab": "applied"}
            )
            response = client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "create_route", "arguments": CREATE_ARGS},
                },
            )
        assert response.status_code == 200
        result = parse_sse(response)["result"]
        assert result["isError"] is False
        mock_service.create_route.assert_called_once()

    def test_create_route_sync_error_is_tool_result(self, client):
        # CONTRACT: TestMcpTools->SyncError->IsErrorToolResult
        headers = mcp_session(client)
        with (
            patch("src.mcp_server.sync_service") as mock_service,
            patch("src.mcp_server.log_tool_call", new=AsyncMock()),
        ):
            mock_service.create_route = AsyncMock(
                side_effect=SyncError(
                    error="GitLab step failed after APISIX route creation",
                    apisix="reverted",
                    gitlab="failed",
                    detail="boom",
                )
            )
            response = client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "create_route", "arguments": CREATE_ARGS},
                },
            )
        assert response.status_code == 200
        result = parse_sse(response)["result"]
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["apisix"] == "reverted"
        assert payload["gitlab"] == "failed"
