# tests/unit/test_mcp_server.py
"""Unit tests for the FastMCP server and tool registrations."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

from src.mcp_server import mcp
from src.services.sync_service import SyncError

EXPECTED_TOOLS = {
    "apisix_read",
    "mpsdk_read",
    "create_route",
    "update_route",
    "deprecate_route",
    "delete_route",
    "report_issue",
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


@pytest.fixture
def mock_service():
    # CONTRACT: Fixture->MockService->PatchSyncServiceAndLogging
    with (
        patch("src.mcp_server.sync_service") as mock,
        patch("src.mcp_server.log_tool_call", new=AsyncMock()) as mock_log,
    ):
        mock.read_apisix = AsyncMock(return_value={"found": True, "object": {}})
        mock.read_mpsdk = AsyncMock(return_value={"found": True, "source": "def f(): ..."})
        for method in (
            "create_route",
            "update_route",
            "deprecate_route",
            "delete_route",
            "report_issue",
        ):
            setattr(mock, method, AsyncMock(return_value={"status": "ok"}))
        mock.log = mock_log
        yield mock


class TestToolRegistration:
    """Test tool registration and annotations."""

    @pytest.mark.asyncio
    async def test_six_tools_registered(self):
        # CONTRACT: TestToolRegistration->ListTools->SixTools
        async with Client(mcp) as client:
            tools = await client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS

    @pytest.mark.asyncio
    async def test_read_tools_are_read_only(self):
        # CONTRACT: TestToolRegistration->ReadTools->ReadOnlyHint
        async with Client(mcp) as client:
            tools = await client.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert by_name["apisix_read"].annotations.readOnlyHint is True
        assert by_name["mpsdk_read"].annotations.readOnlyHint is True

    @pytest.mark.asyncio
    async def test_delete_route_is_destructive(self):
        # CONTRACT: TestToolRegistration->DeleteRoute->DestructiveHint
        async with Client(mcp) as client:
            tools = await client.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert by_name["delete_route"].annotations.destructiveHint is True

    @pytest.mark.asyncio
    async def test_tools_have_descriptions(self):
        # CONTRACT: TestToolRegistration->Descriptions->NonEmpty
        async with Client(mcp) as client:
            tools = await client.list_tools()
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"


class TestToolCalls:
    """Test tool invocation over the in-memory MCP transport."""

    @pytest.mark.asyncio
    async def test_apisix_read_success(self, mock_service):
        # CONTRACT: TestToolCalls->ApisixRead->ReturnStructuredResult
        async with Client(mcp) as client:
            result = await client.call_tool(
                "apisix_read", {"params": {"kind": "route", "object_id": "users:{id}"}}
            )
        assert result.is_error is False
        assert result.data == {"found": True, "object": {}}
        mock_service.read_apisix.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_route_success_logs_metrics(self, mock_service):
        # CONTRACT: TestToolCalls->CreateRoute->LogSuccessMetric
        async with Client(mcp) as client:
            result = await client.call_tool("create_route", CREATE_ARGS)
        assert result.is_error is False
        assert result.data["status"] == "ok"
        mock_service.log.assert_called_once()
        log_args = mock_service.log.call_args[0]
        assert log_args[0] == "create_route"
        assert log_args[1] == "/users/{id}"
        assert log_args[2] == "success"

    @pytest.mark.asyncio
    async def test_sync_error_returns_structured_is_error(self, mock_service):
        # CONTRACT: TestToolCalls->SyncError->StructuredIsErrorResult
        mock_service.create_route = AsyncMock(
            side_effect=SyncError(
                error="GitLab step failed after APISIX route creation",
                apisix="reverted",
                gitlab="failed",
                detail="boom",
            )
        )
        async with Client(mcp) as client:
            result = await client.call_tool("create_route", CREATE_ARGS, raise_on_error=False)
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload == {
            "error": "GitLab step failed after APISIX route creation",
            "apisix": "reverted",
            "gitlab": "failed",
            "detail": "boom",
        }
        assert mock_service.log.call_args[0][2] == "compensated"

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_is_error(self, mock_service):
        # CONTRACT: TestToolCalls->UnexpectedError->IsErrorResult
        mock_service.create_route = AsyncMock(side_effect=RuntimeError("unexpected"))
        async with Client(mcp) as client:
            result = await client.call_tool("create_route", CREATE_ARGS, raise_on_error=False)
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload["detail"] == "unexpected"
        assert mock_service.log.call_args[0][2] == "error"

    @pytest.mark.asyncio
    async def test_delete_route_success(self, mock_service):
        # CONTRACT: TestToolCalls->DeleteRoute->ReturnOk
        async with Client(mcp) as client:
            result = await client.call_tool(
                "delete_route",
                {
                    "params": {
                        "uri": "/users/{id}",
                        "method": "GET",
                        "function_name": "get_user",
                        "function_path": "mpsdk/users.py",
                    }
                },
            )
        assert result.is_error is False
        mock_service.delete_route.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_issue_success(self, mock_service):
        # CONTRACT: TestToolCalls->ReportIssue->ReturnOk
        async with Client(mcp) as client:
            result = await client.call_tool(
                "report_issue",
                {
                    "params": {
                        "anchor": "https://docs#rate-limit",
                        "fragment": "rate limit config section",
                        "description": "Cannot tell which unit the period uses",
                    }
                },
            )
        assert result.is_error is False
        assert result.data["status"] == "ok"
        mock_service.report_issue.assert_called_once()
        assert mock_service.log.call_args[0][0] == "report_issue"
        assert mock_service.log.call_args[0][1] == "https://docs#rate-limit"
