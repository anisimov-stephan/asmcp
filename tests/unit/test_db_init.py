# tests/unit/test_db_init.py
"""Unit tests for database initialization."""

from unittest.mock import AsyncMock, patch

import pytest

from src.utils.db_init import ensure_micro_tasks_schema


class TestEnsureMicroTasksSchema:
    # CONTRACT: TestEnsureMicroTasksSchema->VerifyInit->CheckDbCalled
    """Test ensure_micro_tasks_schema function."""

    @pytest.mark.asyncio
    async def test_ensure_schema_calls_init_db(self):
        # CONTRACT: TestEnsureMicroTasksSchema->CallsEnsureSchema->VerifyInvocation
        with patch("src.utils.db_init.ensure_schema", new_callable=AsyncMock) as mock_ensure_schema:
            await ensure_micro_tasks_schema()
            mock_ensure_schema.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_ensure_schema_propagates_init_db_error(self):
        # CONTRACT: TestEnsureMicroTasksSchema->DbError->PropagateException
        with (
            patch(
                "src.utils.db_init.ensure_schema",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Connection refused"),
            ),
            pytest.raises(RuntimeError, match="Connection refused"),
        ):
            await ensure_micro_tasks_schema()

    @pytest.mark.asyncio
    async def test_ensure_schema_idempotent(self):
        # CONTRACT: TestEnsureMicroTasksSchema->Idempotent->CallOncePerInvocation
        with patch("src.utils.db_init.ensure_schema", new_callable=AsyncMock) as mock_ensure_schema:
            await ensure_micro_tasks_schema()
            await ensure_micro_tasks_schema()
            assert mock_ensure_schema.call_count == 2
