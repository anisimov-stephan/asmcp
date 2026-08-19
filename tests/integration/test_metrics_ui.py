# tests/integration/test_metrics_ui.py
"""Integration tests for the landing page and metrics dashboard UI."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture
def client():
    # CONTRACT: Fixture->CreateClient->ReturnTestClient
    return TestClient(app)


METRICS = {
    "total": 3,
    "by_outcome": {"success": 2, "error": 1},
    "by_tool": [{"tool_name": "create_route", "count": 3, "avg_duration_ms": 12}],
    "recent": [
        {
            "tool_name": "create_route",
            "target_uri": "/users/{id}",
            "outcome": "success",
            "duration_ms": 10,
            "created_at": datetime(2026, 1, 1, 12, 0, 0),
        }
    ],
}


class TestLandingPage:
    """Test GET / endpoint."""

    def test_landing_page_success(self, client):
        # CONTRACT: TestLandingPage->Success->ReturnHTML
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_landing_page_contains_navigation(self, client):
        # CONTRACT: TestLandingPage->Navigation->IncludeLinks
        response = client.get("/")
        html = response.text
        assert "/metrics" in html
        assert "/docs" in html
        assert "/mcp" in html


class TestMetricsPage:
    """Test GET /metrics endpoint."""

    def test_metrics_page_success(self, client):
        # CONTRACT: TestMetricsPage->Success->ReturnHTML
        with patch("src.ui.metrics_ui.get_metrics_summary", new=AsyncMock(return_value=METRICS)):
            response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_metrics_page_renders_metrics(self, client):
        # CONTRACT: TestMetricsPage->Render->ShowMetricsData
        with patch("src.ui.metrics_ui.get_metrics_summary", new=AsyncMock(return_value=METRICS)):
            response = client.get("/metrics")
        html = response.text
        assert "create_route" in html
        assert "/users/{id}" in html
        assert "success" in html

    def test_metrics_page_empty(self, client):
        # CONTRACT: TestMetricsPage->Empty->RenderEmptyState
        empty = {"total": 0, "by_outcome": {}, "by_tool": [], "recent": []}
        with patch("src.ui.metrics_ui.get_metrics_summary", new=AsyncMock(return_value=empty)):
            response = client.get("/metrics")
        assert response.status_code == 200
        assert "No tool calls recorded yet." in response.text
