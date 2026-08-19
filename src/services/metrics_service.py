# src/services/metrics_service.py
"""Tool call metrics: logging and aggregation backed by the tool_calls table."""

from typing import Any

from loguru import logger
from micro.shared.db import get_session_context
from sqlmodel import col, func, select

from src.models.tool_calls_table import ToolCallsTable


async def log_tool_call(tool_name: str, target_uri: str, outcome: str, duration_ms: int) -> None:
    # CONTRACT: MetricsService->LogToolCall->InsertToolCallRow
    """Log a tool invocation; metrics failures never break the tool itself."""
    logger.debug(
        f"[metrics_service:LOG_TOOL_CALL:ENTER] tool_name={tool_name}, outcome={outcome}, "
        f"duration_ms={duration_ms}"
    )
    try:
        async with get_session_context() as session:
            session.add(
                ToolCallsTable(
                    tool_name=tool_name,
                    target_uri=target_uri,
                    outcome=outcome,
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.error(f"[metrics_service:LOG_TOOL_CALL:ERROR] error={exc}")
    logger.debug(f"[metrics_service:LOG_TOOL_CALL:EXIT] tool_name={tool_name}")


async def get_metrics_summary(recent_limit: int = 20) -> dict[str, Any]:
    # CONTRACT: MetricsService->GetMetricsSummary->AggregateToolCalls
    """Aggregate tool call metrics for the dashboard."""
    logger.debug("[metrics_service:GET_METRICS_SUMMARY:ENTER]")
    async with get_session_context() as session:
        total_result = await session.exec(select(func.count()).select_from(ToolCallsTable))
        total = total_result.one()

        by_outcome_result = await session.exec(
            select(ToolCallsTable.outcome, func.count()).group_by(ToolCallsTable.outcome)
        )
        by_outcome = {row[0]: row[1] for row in by_outcome_result.all()}

        by_tool_result = await session.exec(
            select(
                ToolCallsTable.tool_name,
                func.count(),
                func.avg(ToolCallsTable.duration_ms),
            ).group_by(ToolCallsTable.tool_name)
        )
        by_tool = [
            {
                "tool_name": row[0],
                "count": row[1],
                "avg_duration_ms": round(row[2]) if row[2] is not None else None,
            }
            for row in by_tool_result.all()
        ]

        recent_result = await session.exec(
            select(ToolCallsTable).order_by(col(ToolCallsTable.id).desc()).limit(recent_limit)
        )
        recent = list(recent_result.all())

    logger.debug(f"[metrics_service:GET_METRICS_SUMMARY:EXIT] total={total}, recent={len(recent)}")
    return {
        "total": total,
        "by_outcome": by_outcome,
        "by_tool": by_tool,
        "recent": recent,
    }
