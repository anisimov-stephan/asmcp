# src/ui/metrics_ui.py
"""UI router for landing page and tool call metrics dashboard."""

from pathlib import Path

import micro
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from loguru import logger

from src.services.metrics_service import get_metrics_summary

metrics_ui_router = APIRouter(tags=["ui"], include_in_schema=False)

_base_dir = Path(__file__).resolve().parent
_jinja_env = Environment(loader=FileSystemLoader(str(_base_dir / "templates")))


# CONTRACT: Router->RenderTemplate->ReturnHTML
def render_template(template_name: str, **kwargs) -> str:
    """Render template with common context."""
    kwargs.setdefault("app_version", micro.get_version())
    kwargs.setdefault("app_header", micro.get_project_header())
    kwargs.setdefault("app_description", micro.get_project_description())
    template = _jinja_env.get_template(template_name)
    return template.render(**kwargs)


# CONTRACT: Router->LandingPage->RenderLanding
@metrics_ui_router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing_page(request: Request) -> HTMLResponse:
    """Landing page with project info and navigation."""
    logger.debug("[ui:LANDING_PAGE] request_url=%s", str(request.url))
    html = render_template("landing.html", request=request)
    return HTMLResponse(html)


# CONTRACT: Router->MetricsPage->RenderMetrics
@metrics_ui_router.get("/metrics", response_class=HTMLResponse, include_in_schema=False)
async def metrics_page(request: Request) -> HTMLResponse:
    """Metrics dashboard backed by the tool_calls table."""
    logger.debug("[ui:METRICS_PAGE] request_url=%s", str(request.url))
    metrics = await get_metrics_summary()
    html = render_template("metrics.html", request=request, metrics=metrics)
    return HTMLResponse(html)
