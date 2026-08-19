# src/utils/db_init.py
"""Database initialization for micro tasks."""

from loguru import logger
from micro.shared.db import ensure_schema


async def ensure_micro_tasks_schema() -> None:
    """Ensure micro_tasks table exists."""
    # CONTRACT: DBInit->EnsureMicroTasksSchema->CreateTables
    logger.debug("[ensure_micro_tasks_schema:ENTER]")

    await ensure_schema()

    logger.debug("[ensure_micro_tasks_schema:EXIT] schema_ready=True")
