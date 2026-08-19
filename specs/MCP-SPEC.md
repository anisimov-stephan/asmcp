# APISIX-MCP Development Specification

This specification is for the development of an application that exposes MCP tools for an AI agent.

The application is based on Micro core and the files provided in this repository as a microservice template. Pre-existing technical solutions present here should be used extensively. Per the template's README.md and AGENTS.md, this application is stripped of demo functionality as described in the Architecture section.

The tools provided by this service act upon a corporate APISIX instance and an instance of GitLab, where:

1. APISIX acts as an API gateway for external APIs, providing a single point of access, as well as rate-limiting, monitoring and secret management;
2. GitLab hosts a repository called MPSDK which in turn provides convenient wrappers for requests to the aforementioned APISIX instance to streamline external API communication carried out by other internal Python services.

Refer to APISIX's official docs (3.x Admin API) and GitLab REST API v4 docs for API reference.

Each mutating tool uses both the APISIX Admin API and GitLab's API to synchronize updates between APISIX and MPSDK. The target process is as follows:

1. A method of an external API gets updated and the change is documented in a corresponding update feed;
2. An agent acting outside the scope of this application extracts relevant updates from the feed;
3. The same agent uses tools provided by this application to add/patch/deprecate a route in APISIX and create/update/deprecate a function in MPSDK, with changes delivered through a daily merge request.

## MCP Server

- **SDK:** FastMCP (pinned in `pyproject.toml` dependencies).
- **Transport:** Streamable HTTP, mounted as an ASGI sub-app inside the existing FastAPI application at `/mcp`. The FastMCP session-manager lifespan is combined with the existing `app_lifespan` in `src/main.py`.
- **Auth:** `/mcp` is protected by the existing Keycloak JWT middleware (it is NOT added to `exclude_auth_paths`). The calling agent authenticates with `Authorization: Bearer <jwt>`.
- **Tool contract:** each tool has a Pydantic input model, a description string, and MCP annotations (`readOnlyHint: true` for the read tools, `destructiveHint: true` for `delete_route`).
- **Errors:** failures are returned as tool results with `isError: true` and a structured message, never as MCP protocol errors:

```json
{
    "error": "<human-readable message>",
    "apisix": "applied | reverted | untouched | failed",
    "gitlab": "applied | failed | untouched",
    "detail": "<original exception text>"
}
```

### Tools

Seven tools are exposed:

1. **`apisix_read`** — used freely to access the current state of an APISIX route/service/upstream.
2. **`mpsdk_read`** — used freely to access the current state of an MPSDK function.
3. **`create_route`** — creates a route in APISIX while simultaneously writing an MPSDK function and committing it to GitLab. Fails if the route already exists.
4. **`update_route`** — updates a route in APISIX while simultaneously editing an MPSDK function and committing it to GitLab. Fails if the route does not exist.
5. **`deprecate_route`** — marks a route in APISIX as deprecated (adding a `DEPRECATED: ` prefix before the route's name) while simultaneously marking the corresponding MPSDK function with typing-extensions' `@deprecated` decorator.
6. **`delete_route`** — declares that a method is terminally removed from the target external API. Deletes nothing: sets the APISIX route's `status` to `0` and commits a change to `TO-BE-DELETED.md` at the MPSDK project root recording which methods are cut from the target API. Physical removal of routes and functions is out of scope (manual cleanup).
7. **`report_issue`** — reports that a documentation fragment is unclear or not actionable (e.g. it is not clear how to configure a specific rate limit). GitLab-only: records the fragment in `NEEDS-REVIEW.md` (see below). Used instead of guessing; no APISIX changes are made.

All incoming parameters are supplied in JSON by the agent analyzing a given feed. The data provided by the agent is:

1. **URI** — the URI of an endpoint to be accessed
2. **Anchor link** — the anchor link to the corresponding documentation section
3. **Method** — HTTP method to access the endpoint
4. **Optional per-route rate-limit plugin settings** — period and request count
5. **Service ID** — the internal id of a service (corresponds to an external API provider)
6. **Function signature** — full code of a function to be created/updated
7. **Function location** — the path to a file where the function should be created/updated

## Configuration

The application is deployed via GitLab CI with environment variables; all app settings are read from the process environment. For local development, `make dev` passes `.env.dev` to uvicorn via `--env-file`. An `.env.example` file at the repository root lists every variable. Core micro settings (`DATABASE_URL`, `KEYCLOAK_URL`, ...) still go through the micro `Settings` mechanism.

| Variable | Purpose |
|---|---|
| `APISIX_ADMIN_URL` | Base URL of the APISIX Admin API |
| `APISIX_ADMIN_KEY` | Admin API key, sent as the `X-API-KEY` header |
| `GITLAB_URL` | Base URL of the GitLab instance |
| `GITLAB_TOKEN` | Project access token scoped to the MPSDK project only |
| `MPSDK_PROJECT_ID` | GitLab project id of the MPSDK repository |
| `MPSDK_BASE_BRANCH` | Branch daily update branches are cut from (default `main`) |
| `EXTERNAL_HTTP_TIMEOUT_S` | Timeout for APISIX/GitLab HTTP calls (default `30`) |

GitLab access is restricted to the MPSDK repository by the token's scope; additionally, no tool input accepts a project parameter — all GitLab operations address `MPSDK_PROJECT_ID` exclusively.

## APISIX Side

Target: APISIX 3.x Admin API. All calls go through a single async `httpx` client (`src/clients/apisix_client.py`).

### Route identification

- **Route id / name normalization:** strip the leading slash, replace `/` with `:`, keep curly braces around path parameters. Example: `/users/{id}/orders` → id `users:{id}:orders`.
- **URI field:** path parameters are converted to APISIX path variables (`{param}` → `:param`). Example: `/users/{id}/orders` → `uri: /users/:id/orders`.
- At creation, `name == id` exactly. Later mutations (deprecation) may add a tag to `name`; the id and uri never change, so the route always stays addressable by id.

### Write operations

All writes use **PUT** (read-merge-write where the route already exists). PATCH is not used.

- **Create** (`PUT /apisix/admin/routes/<id>`):

```json
{
    "name": "<same as id>",
    "desc": "<anchor link>",
    "uri": "<URI with :param path variables>",
    "methods": ["<method>"],
    "service_id": "<service ID>",
    "plugins": {
        "limit-count": {
            "policy": "local",
            "count": "<rate-limit request count>",
            "rejected_msg": "rate-limit reached for <name>",
            "time_window": "<rate-limit period>",
            "rejected_code": 429
        }
    }
}
```

Rate-limit settings are optional: when omitted, the `plugins` block is left out entirely.

- **Update:** GET the current route, merge only the provided fields into the snapshot, PUT the full object back. Omitted rate-limit settings preserve the existing `limit-count` plugin untouched.
- **Deprecate:** PUT with `name` set to `DEPRECATED: <previous name>`. The equality of `name` and `id` holds only at creation; the deprecation tag breaks it by design.
- **Delete:** PUT with `"status": 0`. The route remains in APISIX, disabled.

### Read operation

`apisix_read` performs a GET by id against routes, services, or upstreams and returns the raw APISIX object JSON. Disabled (`status: 0`) routes are returned with their status visible.

## GitLab / MPSDK Side

All calls go through a single async `httpx` client (`src/clients/gitlab_client.py`) against GitLab REST API v4.

### Branching and merge requests

- All changes for a given update session are committed to the same `update-DD-MM-YYYY` branch (UTC date), cut from `MPSDK_BASE_BRANCH`. The branch is created if absent, reused if present.
- Each mutating tool ensures **exactly one open merge request per daily branch**: on commit, it checks for an open MR from that branch; if none exists, it creates one (title `MPSDK update DD-MM-YYYY`, target `MPSDK_BASE_BRANCH`), otherwise it reuses the existing one. The agent never opens additional MRs; the dev accepting the MR squashes the commits.

### Function writes

- Function signature and location are used to insert the function into the required location in MPSDK. Writes are AST-based upserts: if a function with the same name exists in the target file, its node is replaced; otherwise the function is appended to the end of the file. If the location file does not exist, it is created.
- **Deprecation** inserts `@deprecated("<message>")` from `typing_extensions` directly above the `def`, adding the import if absent.
- Every commit also bumps the MPSDK **patch** version in its `pyproject.toml`. Minor versions are reserved for terminal function deletions (manual cleanups), major versions for contract changes.
- **`TO-BE-DELETED.md`** (project root) is append-only, one entry per line:

```
- <METHOD> <URI> — <function name> in <path> (marked YYYY-MM-DD)
```

Duplicate entries are skipped.

### Needs-review reports

`report_issue` does not touch the daily update branch. It commits to a separate `needs-review-DD-MM-YYYY` branch (UTC date), cut from `MPSDK_BASE_BRANCH` (created if absent, reused if present), with its own single open MR per day (title `MPSDK needs review DD-MM-YYYY`, target `MPSDK_BASE_BRANCH`). The commit contains **only** `NEEDS-REVIEW.md` — no version bump, no other changes. `NEEDS-REVIEW.md` (project root) is append-only, one entry per reported fragment; exact duplicates are skipped:

```
## <anchor link> (reported YYYY-MM-DD)
- URI: <uri or "—"> <method or "">
- Fragment: <fragment text>
- Problem: <what is unclear>
```

### Commit messages

One commit per tool call, containing the file change and the version bump:

| Tool | Message |
|---|---|
| `create_route` | `feat: add <uri>` |
| `update_route` | `fix: update <uri>` |
| `deprecate_route` | `deprecate: <uri>` |
| `delete_route` | `chore: mark <uri> to-be-deleted` |
| `report_issue` | `docs: flag <anchor> for review` |

## Failure Semantics

- Order: **APISIX first, GitLab second**, for every route-mutating tool.
- A pre-change snapshot of the route is taken before writing, enabling compensation when the GitLab step fails:
  - `create_route` → DELETE the just-created APISIX route;
  - `update_route` / `deprecate_route` / `delete_route` → PUT the snapshot back.
- The tool then returns an `isError` result reporting the per-system state (`apisix: applied|reverted`, `gitlab: failed`) so the agent can retry safely.
- `report_issue` touches GitLab only; nothing is applied to APISIX, so there is nothing to compensate (`apisix: untouched` on failure).
- **Idempotency:** APISIX PUT is naturally idempotent; the GitLab side is check-before-commit — if the resulting file content is unchanged, no commit is made.

## Architecture

### Template changes

- **Removed:** `src/routers/demo_api.py`, `src/plans/demo_plans.py`, all `hello_*` models/services/repository/table, and their tests.
- **Dropped:** the micro tasks/plans machinery — `src/workers/micro_worker.py` is deleted, no plans are registered, and `app_lifespan` only starts the MCP session manager, the DB schema init, and telemetry (the request-logging middleware and `/micro` UI still function; the task/plan pages are inert).
- **Kept:** `src/ui/` — the demo UI is repurposed as a **metrics dashboard** backed by the `tool_calls` table; templates are reworked accordingly. Auth, DB and telemetry machinery from the template stay as-is.

### Persistence

The database is retained for micro core tasks and for metrics. New SQLModel table `tool_calls` (`src/models/tool_calls_table.py`) logging every tool invocation:

- `tool_name`
- `target_uri`
- `outcome` (`success` / `error` / `compensated`)
- `duration_ms`
- `created_at`

### New modules

| Module | Responsibility |
|---|---|
| `src/clients/apisix_client.py` | Async httpx client for the APISIX Admin API |
| `src/clients/gitlab_client.py` | Async httpx client for GitLab REST API v4 (MPSDK only) |
| `src/services/sync_service.py` | Orchestration: ordering, snapshots, compensation |
| `src/services/metrics_service.py` | Tool-call metrics: logging and aggregation |
| `src/mcp_server.py` | FastMCP instance and tool registrations |
| `src/models/tool_calls_table.py` | SQLModel table for tool-call metrics |

## Testing

- All external HTTP is mocked (`respx` or `httpx.MockTransport`) — no real APISIX or GitLab calls in unit/integration tests.
- Fixtures provide APISIX route JSON and GitLab commit/MR API responses.
- Per-tool tests cover the happy path and every compensation path.
- One MCP-level integration test exercises the tools over the mounted app.
