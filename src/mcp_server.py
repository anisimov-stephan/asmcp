# src/mcp_server.py
"""FastMCP server instance and tool registrations."""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from mcp.types import ToolAnnotations

from src.services.metrics_service import log_tool_call
from src.services.sync_service import (
    ApisixReadInput,
    CreateRouteInput,
    DeleteRouteInput,
    DeprecateRouteInput,
    MpsdkReadInput,
    ReportIssueInput,
    SyncError,
    UpdateRouteInput,
    sync_service,
)

mcp = FastMCP("apisix-mcp")


async def _run_tool(
    tool_name: str, target_uri: str, action: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    # CONTRACT: McpServer->RunTool->ExecuteAndLogMetrics
    """Execute a tool action, log metrics, map failures to structured isError results."""
    logger.debug(f"[mcp:RUN_TOOL:ENTER] tool_name={tool_name}, target_uri={target_uri}")
    started = time.monotonic()
    outcome = "success"
    try:
        result = await action()
        logger.debug(f"[mcp:RUN_TOOL:EXIT] tool_name={tool_name}, outcome=success")
        return result
    except SyncError as exc:
        outcome = "compensated" if exc.apisix == "reverted" else "error"
        logger.error(
            f"[mcp:RUN_TOOL:ERROR] tool_name={tool_name}, outcome={outcome}, error={exc.error}"
        )
        raise ToolError(json.dumps(exc.to_payload(), ensure_ascii=False)) from exc
    except Exception as exc:
        outcome = "error"
        logger.error(f"[mcp:RUN_TOOL:ERROR] tool_name={tool_name}, error={exc}")
        payload = {
            "error": "Unexpected tool failure",
            "apisix": "untouched",
            "gitlab": "untouched",
            "detail": str(exc),
        }
        raise ToolError(json.dumps(payload, ensure_ascii=False)) from exc
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        await log_tool_call(tool_name, target_uri, outcome, duration_ms)


@mcp.tool(
    description=(
        "Read the current state of an APISIX route, service or upstream by id. "
        "Returns the raw APISIX object JSON; disabled routes are returned with status 0."
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def apisix_read(params: ApisixReadInput) -> dict[str, Any]:
    # CONTRACT: McpServer->ApisixRead->ReadApisixObject
    return await _run_tool(
        "apisix_read",
        params.object_id,
        lambda: sync_service.read_apisix(params),
    )


@mcp.tool(
    description=(
        "Read the current state of an MPSDK function: its source code in the target file, "
        "read from today's update branch when present, otherwise from the base branch."
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def mpsdk_read(params: MpsdkReadInput) -> dict[str, Any]:
    # CONTRACT: McpServer->MpsdkRead->ReadMpsdkFunction
    return await _run_tool(
        "mpsdk_read",
        params.function_path,
        lambda: sync_service.read_mpsdk(params),
    )


@mcp.tool(
    description=(
        "Create a route in APISIX and simultaneously write the MPSDK wrapper function, "
        "committing it to the daily update branch in GitLab. Fails if the route already exists."
    )
)
async def create_route(params: CreateRouteInput) -> dict[str, Any]:
    # CONTRACT: McpServer->CreateRoute->SyncCreate
    return await _run_tool(
        "create_route",
        params.uri,
        lambda: sync_service.create_route(params),
    )


@mcp.tool(
    description=(
        "Update a route in APISIX and simultaneously edit the MPSDK wrapper function, "
        "committing it to the daily update branch in GitLab. Fails if the route does not exist."
    )
)
async def update_route(params: UpdateRouteInput) -> dict[str, Any]:
    # CONTRACT: McpServer->UpdateRoute->SyncUpdate
    return await _run_tool(
        "update_route",
        params.uri,
        lambda: sync_service.update_route(params),
    )


@mcp.tool(
    description=(
        "Mark a route in APISIX as deprecated (DEPRECATED: name prefix) and mark the "
        "corresponding MPSDK function with the typing_extensions @deprecated decorator."
    )
)
async def deprecate_route(params: DeprecateRouteInput) -> dict[str, Any]:
    # CONTRACT: McpServer->DeprecateRoute->SyncDeprecate
    return await _run_tool(
        "deprecate_route",
        params.uri,
        lambda: sync_service.deprecate_route(params),
    )


@mcp.tool(
    description=(
        "Declare that a method is terminally removed from the target external API. "
        "Deletes nothing: disables the APISIX route (status 0) and records the method "
        "in TO-BE-DELETED.md at the MPSDK project root."
    ),
    annotations=ToolAnnotations(destructiveHint=True),
)
async def delete_route(params: DeleteRouteInput) -> dict[str, Any]:
    # CONTRACT: McpServer->DeleteRoute->SyncDelete
    return await _run_tool(
        "delete_route",
        params.uri,
        lambda: sync_service.delete_route(params),
    )


@mcp.tool(
    description=(
        "Report that a documentation fragment is unclear or not actionable (e.g. it is "
        "not clear how to configure a specific rate limit). Records the fragment in "
        "NEEDS-REVIEW.md at the MPSDK project root on a dedicated needs-review branch "
        "with its own merge request. Use this instead of guessing."
    )
)
async def report_issue(params: ReportIssueInput) -> dict[str, Any]:
    # CONTRACT: McpServer->ReportIssue->FlagFragment
    return await _run_tool(
        "report_issue",
        params.anchor,
        lambda: sync_service.report_issue(params),
    )


mcp_app = mcp.http_app(path="/")
