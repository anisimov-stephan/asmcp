# src/main.py
"""Application entry point."""

from contextlib import asynccontextmanager

import micro
import micro.shared.api.middleware as micro_middleware
from fastapi import Request, Response
from loguru import logger
from micro import create_app
from micro.api.services import telemetry_service

from src.mcp_server import mcp_app  # noqa: E402
from src.ui.metrics_ui import metrics_ui_router  # noqa: E402
from src.utils.config import get_project_description, get_project_header  # noqa: E402
from src.utils.db_init import ensure_micro_tasks_schema  # noqa: E402

_original_request_logging_middleware = micro_middleware.request_logging_middleware


async def _request_logging_middleware_skip_mcp(request: Request, call_next) -> Response:
    # CONTRACT: Middleware->SkipMcpBodyReplay->PreserveStreamableHttp
    """Skip request-body logging for /mcp: replaying the body breaks the MCP transport."""
    if request.url.path.startswith("/mcp"):
        micro_middleware.telemetry_service.increment_request_count()
        return await call_next(request)
    return await _original_request_logging_middleware(request, call_next)


micro_middleware.request_logging_middleware = _request_logging_middleware_skip_mcp


@asynccontextmanager
async def app_lifespan(app):
    # CONTRACT: App->StartServices->LaunchMcpAndTelemetry
    async with mcp_app.lifespan(app):
        logger.info("[app:LIFESPAN:START] mcp_session_manager_started=True")

        logger.info("[app:LIFESPAN:START] initializing database schema")
        await ensure_micro_tasks_schema()
        logger.info("[app:LIFESPAN:START] schema_ready=True")

        logger.info("[app:LIFESPAN:START] starting telemetry service")
        telemetry_service.start()
        logger.info("[app:LIFESPAN:START] telemetry_started=True")

        yield

        # CONTRACT: App->StopServices->TerminateTelemetry
        logger.info("[app:LIFESPAN:STOP] stopping telemetry service")
        telemetry_service.stop()
        logger.info("[app:LIFESPAN:STOP] telemetry_stopped=True")


app = create_app(
    routers=[metrics_ui_router],
    templates_dir="src/ui/templates",
    static_dir=str(micro.get_static_dir()),
    enable_health=True,
    enable_auth=True,
    exclude_auth_paths=["/metrics", "/micro", "/api/v1/micro/plans", "/api/v1/micro/plans/*"],
)
app.mount("/mcp", mcp_app)
app.router.lifespan_context = app_lifespan
app.title = get_project_header()
app.description = get_project_description()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
