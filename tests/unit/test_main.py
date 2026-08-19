# tests/unit/test_main.py
"""Unit tests for main app module."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import app, app_lifespan


@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest.fixture
def mock_mcp_app():
    """Stub the FastMCP sub-app lifespan."""
    stub = MagicMock()
    stub.lifespan = _noop_lifespan
    with patch("src.main.mcp_app", stub):
        yield stub


class TestAppLifespan:
    """Test app lifespan startup and shutdown."""

    @pytest.mark.asyncio
    async def test_lifespan_startup(self, mock_mcp_app):
        # CONTRACT: TestAppLifespan->Startup->InitSchemaAndTelemetry
        with (
            patch("src.main.ensure_micro_tasks_schema", new_callable=AsyncMock) as mock_db,
            patch("src.main.telemetry_service") as mock_telemetry,
        ):
            async with app_lifespan(None):
                mock_db.assert_called_once()
                mock_telemetry.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_shutdown(self, mock_mcp_app):
        # CONTRACT: TestAppLifespan->Shutdown->StopTelemetry
        with (
            patch("src.main.ensure_micro_tasks_schema", new_callable=AsyncMock),
            patch("src.main.telemetry_service") as mock_telemetry,
        ):
            async with app_lifespan(None):
                pass

            mock_telemetry.stop.assert_called_once()


class TestAppInstance:
    """Test app configuration."""

    def test_app_metrics_ui_router_registered(self):
        # CONTRACT: TestAppInstance->MetricsRouterRegistered->VerifyRoutePath
        route_paths = [getattr(route, "path", "") for route in app.routes]
        assert "/metrics" in route_paths, f"Expected /metrics in routes, got: {route_paths}"

    def test_app_ui_router_registered(self):
        # CONTRACT: TestAppInstance->UIRouterRegistered->VerifyRoutePath
        route_paths = [getattr(route, "path", "") for route in app.routes]
        assert "/" in route_paths, f"Expected / root path in routes, got: {route_paths}"

    def test_app_mcp_mounted(self):
        # CONTRACT: TestAppInstance->McpMounted->VerifyMountPath
        mount_paths = [
            getattr(route, "path", "")
            for route in app.routes
            if route.__class__.__name__ == "Mount"
        ]
        assert "/mcp" in mount_paths, f"Expected /mcp mount in routes, got: {mount_paths}"

    def test_app_title_matches_config(self):
        # CONTRACT: TestAppInstance->TitleMatchesConfig->VerifyFromConfig
        from src.utils.config import get_project_header

        expected = get_project_header()
        assert app.title == expected, f"Expected title '{expected}', got '{app.title}'"

    def test_app_description_matches_config(self):
        # CONTRACT: TestAppInstance->DescriptionMatchesConfig->VerifyFromConfig
        from src.utils.config import get_project_description

        expected = get_project_description()
        assert app.description == expected, (
            f"Expected description '{expected}', got '{app.description}'"
        )

    def test_app_lifespan_attached(self):
        # CONTRACT: TestAppInstance->LifespanAttached->VerifyContext
        assert app.router.lifespan_context is app_lifespan
