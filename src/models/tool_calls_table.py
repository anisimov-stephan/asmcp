# src/models/tool_calls_table.py
"""Tool call metrics table model."""

from datetime import datetime

from sqlalchemy import TIMESTAMP
from sqlmodel import Field, SQLModel


class ToolCallsTable(SQLModel, table=True):
    """MCP tool invocation metrics table."""

    __tablename__ = "tool_calls"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    tool_name: str
    target_uri: str
    outcome: str
    duration_ms: int
    created_at: datetime = Field(  # type: ignore[call-overload]
        default_factory=datetime.utcnow,
        sa_type=TIMESTAMP(timezone=False),
    )
