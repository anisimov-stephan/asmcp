# AGENTS.md

## Commands

### Install

```bash
uv sync --extra dev          # full test stack (pytest, pytest-cov, httpx)
```

`[dependency-groups] dev` contains only `mypy`/`ruff`; the test stack lives in `[project.optional-dependencies] dev`. `--extra dev` gets both.

**`uv run` / `uv sync` can fail:** `micro` is pinned to a git tag on a private GitLab (`[tool.uv.sources]`). If the tag is unfetchable, any `uv run` errors out during re-sync. The existing `.venv` is already populated — use `uv run --no-sync <cmd>` to skip the sync step.

The project is a **virtual** (non-packaged) uv project — tests import `src` via `pythonpath = ["src"]` in pytest config. Never create a top-level package named `src/mcp/` — see "Gotchas".

### Dev & Run

```bash
make dev                     # kills port 8000 first, then uvicorn --reload with MICRO_MODE=dev
make dev 8001                # same on port 8001
```

Core micro vars (`DATABASE_URL`, `KEYCLOAK_URL` — auth; empty → 500 "Keycloak is not configured" on protected endpoints) are loaded from `.env.{MICRO_MODE}` (default `.env.dev`) by `micro.shared.core.config`. App-specific vars are read from the **process environment** (`os.environ`) at call time; locally `make dev` loads `.env.dev` via uvicorn `--env-file`. `.env.example` lists all of them (the `.gitignore` `.env.*` rule has a `!.env.example` negation):

| Variable | Purpose |
|---|---|
| `APISIX_ADMIN_URL` | Base URL of the APISIX Admin API |
| `APISIX_ADMIN_KEY` | Admin API key, sent as `X-API-KEY` |
| `GITLAB_URL` | Base URL of the GitLab instance |
| `GITLAB_TOKEN` | Project access token scoped to the MPSDK project |
| `MPSDK_PROJECT_ID` | GitLab project id of MPSDK |
| `MPSDK_BASE_BRANCH` | Branch daily update branches are cut from (default `main`) |
| `EXTERNAL_HTTP_TIMEOUT_S` | Timeout for APISIX/GitLab HTTP calls (default `30`) |

### Lint / Format / Typecheck

```bash
uv run --no-sync ruff check . --fix
uv run --no-sync ruff format .
```

`make typecheck` (`mypy micro/`) is **broken**: there is no local `micro/` directory (the package is a git dependency in `.venv`), so mypy errors with "cannot read file 'micro'". It checks nothing — never rely on it. There is no working typecheck command for `src/`.

### Test

```bash
make test                                                   # all tests, no -v
uv run --no-sync pytest tests/unit/ -v
uv run --no-sync pytest tests/unit/test_sync_service.py::TestCreateRoute -v
uv run --no-sync pytest tests/unit/ --cov=src --cov-report=term-missing
```

All tests (unit + integration) are fully mocked — no DB, no env vars, no real APISIX/GitLab calls (`httpx.MockTransport` for clients, `AsyncMock` clients for the service, in-memory `fastmcp.Client` plus TestClient JSON-RPC over the mounted app for MCP).

### Pre-commit checklist

```bash
uv run --no-sync ruff check . --fix && uv run --no-sync ruff format .
uv run --no-sync pytest tests/unit/ tests/integration/ -q
```

## Architecture

- **Entry point:** root `main.py` re-exports `src.main:app`.
- **App factory:** `micro.create_app()` (from the `micro` git dependency) wires health, auth, micro-tasks framework, and its own UI at `/micro`. The metrics UI router is passed in `src/main.py`.
- **MCP server:** `src/mcp_server.py` — FastMCP instance (`fastmcp==3.4.7`, pinned) with 7 tools (`apisix_read`, `mpsdk_read`, `create_route`, `update_route`, `deprecate_route`, `delete_route`, `report_issue`); mounted as a streamable-HTTP ASGI sub-app at `/mcp` (endpoint `/mcp/`). `mcp_app.lifespan` (session manager) wraps the existing startup in `app_lifespan`.
- **Sync orchestration:** `src/services/sync_service.py` — APISIX first, GitLab second; pre-write route snapshots; compensation (DELETE on create, PUT-back on update/deprecate/delete); structured `SyncError` → tool results with `isError: true`. AST-based MPSDK function upserts/deprecation, MPSDK patch-version bump, `TO-BE-DELETED.md` entries.
- **Clients:** `src/clients/apisix_client.py` (APISIX 3.x Admin API, PUT-only writes, route id = URI without leading slash with `/`→`:`) and `src/clients/gitlab_client.py` (GitLab v4, MPSDK only, daily `update-DD-MM-YYYY` branch + one open MR per day; `report_issue` uses a separate `needs-review-DD-MM-YYYY` branch + MR with only `NEEDS-REVIEW.md`). Both are singletons with optional injectable `httpx.AsyncClient` for tests.
- **Metrics:** every tool call is logged to the `tool_calls` table (`src/models/tool_calls_table.py`) via `src/services/metrics_service.py`; `/metrics` renders a dashboard (failures never break tools).
- **Lifespan** (`src/main.py:app_lifespan`): MCP session manager → DB schema init → telemetry. Shutdown in reverse. The micro task worker and plan machinery are **not** started (no plans registered; `/micro` task/plan pages are inert).
- **Auth:** Keycloak JWT via `Authorization: Bearer <jwt>` on everything not excluded, **including `/mcp`**, validated locally via JWKS; claims stashed in `request.state.keycloak_claims`. Tests mock `decode_keycloak_token` in both `micro.shared.api.middleware` and `micro.shared.api.auth`. Excluded (`src/main.py`): `/metrics`, `/micro`, `/api/v1/micro/plans`, `/api/v1/micro/plans/*`.
- **`/service` static mount** exists only if `/var/www/services` exists on the host — absent on dev machines, present in deployed environments.
- **DB sessions:** always `from micro.shared.db import get_session_context` (async context manager). Never raw SQLAlchemy sessions.
- **App title/description** come from custom `header`/`description` fields in `pyproject.toml` (`src/utils/config.py`), not the standard `name`/`description`.
## Code Style

- **CONTRACT comment** on every function/method: `# CONTRACT: Actor->Action->Goal` immediately before `def`, no blank line.
- **Logging:** loguru anchors — `logger.debug(f"[ClassName:METHOD:ENTER] key={value}")` with `:EXIT`/`:STEP`/`:ERROR` variants. Never `print()` in `src/`.
- **Types:** Python 3.13 syntax only — `list[str]`, `str | None`. No `typing.List`/`Optional`.
- **Import order:** stdlib → third-party (incl. `micro.*`) → `src.*`, blank line between groups.
- **No comments** except CONTRACT and docstrings.
- **Ruff:** `py313`, line-length 100, `ignore = ["B008"]`, select `E,F,I,N,W,UP,B,C4,SIM`.

## Gotchas

- **Never create `src/mcp/`**: with `pythonpath = ["src"]` (pytest) it shadows the `mcp` PyPI package (the MCP SDK), and FastMCP fails with "FastMCP server support is not installed". The MCP server lives in `src/mcp_server.py` (flat module) for this reason.
- **micro's `request_logging_middleware` breaks streamable HTTP**: it reads POST bodies and replays `http.request` on every `receive()`, which crashes the MCP transport ("Unexpected message received: http.request"). `src/main.py` monkeypatches `micro.shared.api.middleware.request_logging_middleware` to skip `/mcp` before `create_app()` runs — do not remove that patch.
- `/mcp` (no trailing slash) 307-redirects to `/mcp/`; MCP clients follow it.
- Route id ↔ URI: id `users:{id}:orders` (braces kept, `:` separators) vs APISIX `uri: /users/:id/orders` (`{param}` → `:param`). `name == id` only at creation; deprecation prefixes `name`.
- `tool_calls` table is created by micro's `ensure_schema()` (global `SQLModel.metadata.create_all`) at startup — it works because `src/models/tool_calls_table.py` is imported (via `src.mcp_server` → `src.services.sync_service` → ... → `metrics_service`) before lifespan runs. Keep that import chain intact.
- `make typecheck` is broken (see above).
- New routers or template/static dirs must be registered in the `create_app()` call in `src/main.py`.
- `pyproject.toml` contains a GitLab access token in the `micro` git URL — do not quote or propagate it. The custom `header` field in `[project]` is load-bearing (app title). Do not add a `[build-system]` — setuptools rejects the non-standard `header` key, and packaging is unnecessary anyway.
- Version bumps: `make patch` / `make minor` (`uv version --bump`).
