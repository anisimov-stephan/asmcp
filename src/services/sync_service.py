# src/services/sync_service.py
"""Orchestration of APISIX <-> MPSDK synchronization: ordering, snapshots, compensation."""

import ast
import os
import re
from datetime import UTC, datetime
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from src.clients.apisix_client import (
    ApisixClient,
    ApisixError,
    ApisixNotFoundError,
    apisix_path_from_uri,
    route_id_from_uri,
)
from src.clients.gitlab_client import (
    GitLabClient,
    daily_branch_name,
    needs_review_branch_name,
    needs_review_mr_title,
)

DEPRECATED_PREFIX = "DEPRECATED: "
TO_BE_DELETED_PATH = "TO-BE-DELETED.md"
NEEDS_REVIEW_PATH = "NEEDS-REVIEW.md"
PYPROJECT_PATH = "pyproject.toml"
_VERSION_PATTERN = re.compile(r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.MULTILINE)


class ApisixReadInput(BaseModel):
    """Input model for the apisix_read tool."""

    kind: Literal["route", "service", "upstream"] = Field(
        description="Kind of the APISIX object to read"
    )
    object_id: str = Field(description="Id of the route, service or upstream")


class MpsdkReadInput(BaseModel):
    """Input model for the mpsdk_read tool."""

    function_name: str = Field(description="Name of the MPSDK function")
    function_path: str = Field(description="Path to the file containing the function")


class CreateRouteInput(BaseModel):
    """Input model for the create_route tool."""

    uri: str = Field(description="URI of the endpoint, e.g. /users/{id}/orders")
    anchor: str = Field(description="Anchor link to the documentation section")
    method: str = Field(description="HTTP method to access the endpoint")
    service_id: str = Field(description="Internal id of the service (external API provider)")
    function_code: str = Field(description="Full code of the MPSDK function to create")
    function_path: str = Field(description="Path to the file where the function is created")
    rate_limit_count: int | None = Field(
        default=None, description="Rate-limit request count per period"
    )
    rate_limit_period: int | None = Field(default=None, description="Rate-limit period in seconds")


class UpdateRouteInput(BaseModel):
    """Input model for the update_route tool."""

    uri: str = Field(description="URI of the endpoint, e.g. /users/{id}/orders")
    anchor: str | None = Field(default=None, description="Anchor link to the documentation")
    method: str | None = Field(default=None, description="HTTP method to access the endpoint")
    service_id: str | None = Field(default=None, description="Internal id of the service")
    function_code: str = Field(description="Full code of the MPSDK function to update")
    function_path: str = Field(description="Path to the file where the function is updated")
    rate_limit_count: int | None = Field(
        default=None, description="Rate-limit request count per period"
    )
    rate_limit_period: int | None = Field(default=None, description="Rate-limit period in seconds")


class DeprecateRouteInput(BaseModel):
    """Input model for the deprecate_route tool."""

    uri: str = Field(description="URI of the endpoint to deprecate")
    function_name: str = Field(description="Name of the MPSDK function to deprecate")
    function_path: str = Field(description="Path to the file containing the function")
    message: str | None = Field(
        default=None, description="Deprecation message for the @deprecated decorator"
    )


class DeleteRouteInput(BaseModel):
    """Input model for the delete_route tool."""

    uri: str = Field(description="URI of the endpoint terminally removed from the API")
    method: str = Field(description="HTTP method of the removed endpoint")
    function_name: str = Field(description="Name of the MPSDK function being cut")
    function_path: str = Field(description="Path to the file containing the function")


class ReportIssueInput(BaseModel):
    """Input model for the report_issue tool."""

    anchor: str = Field(description="Anchor link to the documentation section")
    fragment: str = Field(description="The documentation fragment that is unclear")
    description: str = Field(description="What exactly is unclear or problematic")
    uri: str | None = Field(default=None, description="URI the fragment appears to describe")
    method: str | None = Field(default=None, description="HTTP method the fragment mentions")


class SyncError(Exception):
    """Structured sync failure, reported to the agent as an isError tool result."""

    def __init__(self, error: str, apisix: str, gitlab: str, detail: str) -> None:
        # CONTRACT: SyncError->Initialize->StoreStates
        super().__init__(error)
        self.error = error
        self.apisix = apisix
        self.gitlab = gitlab
        self.detail = detail

    def to_payload(self) -> dict[str, str]:
        # CONTRACT: SyncError->ToPayload->ReturnStructuredMessage
        return {
            "error": self.error,
            "apisix": self.apisix,
            "gitlab": self.gitlab,
            "detail": self.detail,
        }


def upsert_function_source(source: str | None, function_code: str) -> tuple[str, str]:
    # CONTRACT: SyncService->UpsertFunctionSource->ReplaceOrAppendFunction
    """AST-based upsert: replace the function with the same name or append it."""
    new_tree = ast.parse(function_code)
    fn_node = next(
        (
            node
            for node in new_tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
        None,
    )
    if fn_node is None:
        raise ValueError("function_code contains no function definition")

    if source is None or not source.strip():
        result = function_code if function_code.endswith("\n") else f"{function_code}\n"
        return result, fn_node.name

    tree = ast.parse(source)
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == fn_node.name:
            tree.body[index] = fn_node
            break
    else:
        tree.body.append(fn_node)
    return f"{ast.unparse(tree)}\n", fn_node.name


def deprecate_function_source(source: str, function_name: str, message: str) -> str:
    # CONTRACT: SyncService->DeprecateFunctionSource->AddDeprecatedDecorator
    """Insert @deprecated from typing_extensions above the function, adding the import."""
    tree = ast.parse(source)
    fn_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        ),
        None,
    )
    if fn_node is None:
        raise ValueError(f"Function '{function_name}' not found in target file")

    already_deprecated = any(
        (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Name)
            and dec.func.id == "deprecated"
        )
        or (isinstance(dec, ast.Name) and dec.id == "deprecated")
        for dec in fn_node.decorator_list
    )
    if not already_deprecated:
        decorator = ast.Call(
            func=ast.Name(id="deprecated", ctx=ast.Load()),
            args=[ast.Constant(value=message)],
            keywords=[],
        )
        fn_node.decorator_list.insert(0, decorator)

    typing_import = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "typing_extensions"
        ),
        None,
    )
    if typing_import is None:
        import_node = ast.ImportFrom(
            module="typing_extensions",
            names=[ast.alias(name="deprecated")],
            level=0,
        )
        insert_at = 0
        if tree.body and isinstance(tree.body[0], ast.Expr):
            first_value = tree.body[0].value
            if isinstance(first_value, ast.Constant) and isinstance(first_value.value, str):
                insert_at = 1
        tree.body.insert(insert_at, import_node)
    elif all(alias.name != "deprecated" for alias in typing_import.names):
        typing_import.names.append(ast.alias(name="deprecated"))

    ast.fix_missing_locations(tree)
    return f"{ast.unparse(tree)}\n"


def bump_pyproject_patch(content: str) -> str:
    # CONTRACT: SyncService->BumpPyprojectPatch->IncrementPatchVersion
    """Bump the patch version in pyproject.toml content."""
    if not _VERSION_PATTERN.search(content):
        raise ValueError("No version field found in pyproject.toml")

    def _bump(match: re.Match[str]) -> str:
        major, minor, patch = match.group(2), match.group(3), int(match.group(4))
        return f"{match.group(1)}{major}.{minor}.{patch + 1}{match.group(5)}"

    return _VERSION_PATTERN.sub(_bump, content, count=1)


def to_be_deleted_entry(method: str, uri: str, function_name: str, path: str) -> str:
    # CONTRACT: SyncService->ToBeDeletedEntry->FormatEntryLine
    """Format a TO-BE-DELETED.md entry line."""
    marked = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"- {method} {uri} — {function_name} in {path} (marked {marked})"


def append_to_be_deleted(
    content: str | None, method: str, uri: str, function_name: str, path: str
) -> str:
    # CONTRACT: SyncService->AppendToBeDeleted->AppendEntrySkipDuplicates
    """Append an entry to TO-BE-DELETED.md content, skipping duplicates."""
    entry = to_be_deleted_entry(method, uri, function_name, path)
    if content is None:
        return f"{entry}\n"
    if entry in content:
        return content
    separator = "" if content.endswith("\n") else "\n"
    return f"{content}{separator}{entry}\n"


def needs_review_entry(data: ReportIssueInput) -> str:
    # CONTRACT: SyncService->NeedsReviewEntry->FormatEntryBlock
    """Format a NEEDS-REVIEW.md entry block."""
    reported = datetime.now(UTC).strftime("%Y-%m-%d")
    uri = data.uri or "—"
    method = data.method or ""
    return (
        f"## {data.anchor} (reported {reported})\n"
        f"- URI: {uri} {method}\n"
        f"- Fragment: {data.fragment}\n"
        f"- Problem: {data.description}\n"
    )


def append_needs_review(content: str | None, entry: str) -> str:
    # CONTRACT: SyncService->AppendNeedsReview->AppendEntrySkipDuplicates
    """Append an entry to NEEDS-REVIEW.md content, skipping duplicates."""
    if content is None:
        return entry
    if entry in content:
        return content
    separator = "" if content.endswith("\n") else "\n"
    return f"{content}{separator}\n{entry}"


def extract_function_source(source: str, function_name: str) -> str | None:
    # CONTRACT: SyncService->ExtractFunctionSource->ReturnFunctionCodeOrNone
    """Extract a single function's source from a file's content."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function_name:
            return f"{ast.unparse(node)}\n"
    return None


def build_route_payload(
    uri: str,
    anchor: str,
    method: str,
    service_id: str,
    rate_limit_count: int | None,
    rate_limit_period: int | None,
) -> dict[str, Any]:
    # CONTRACT: SyncService->BuildRoutePayload->AssembleRouteObject
    """Build the APISIX route object for creation."""
    route_id = route_id_from_uri(uri)
    payload: dict[str, Any] = {
        "name": route_id,
        "desc": anchor,
        "uri": apisix_path_from_uri(uri),
        "methods": [method],
        "service_id": service_id,
    }
    if rate_limit_count is not None and rate_limit_period is not None:
        payload["plugins"] = {
            "limit-count": {
                "policy": "local",
                "count": rate_limit_count,
                "rejected_msg": f"rate-limit reached for {route_id}",
                "time_window": rate_limit_period,
                "rejected_code": 429,
            }
        }
    return payload


class SyncService:
    """Orchestrates APISIX and GitLab/MPSDK changes with snapshot compensation."""

    def __init__(
        self,
        apisix: ApisixClient | None = None,
        gitlab: GitLabClient | None = None,
    ) -> None:
        # CONTRACT: SyncService->Initialize->SetupClients
        logger.debug("[SyncService:INIT]")
        from src.clients.apisix_client import apisix_client
        from src.clients.gitlab_client import gitlab_client

        self._apisix = apisix or apisix_client
        self._gitlab = gitlab or gitlab_client

    async def read_apisix(self, data: ApisixReadInput) -> dict[str, Any]:
        # CONTRACT: SyncService->ReadApisix->ReturnRawObject
        logger.debug(
            f"[SyncService:READ_APISIX:ENTER] kind={data.kind}, object_id={data.object_id}"
        )
        try:
            obj = await self._apisix.read(data.kind, data.object_id)
        except ApisixNotFoundError:
            logger.debug(
                f"[SyncService:READ_APISIX:EXIT] kind={data.kind}, "
                f"object_id={data.object_id}, found=False"
            )
            return {"found": False, "kind": data.kind, "id": data.object_id}
        except ApisixError as exc:
            raise SyncError(
                error="Failed to read from APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc
        logger.debug(
            f"[SyncService:READ_APISIX:EXIT] kind={data.kind}, object_id={data.object_id}, "
            f"found=True"
        )
        return {"found": True, "kind": data.kind, "id": data.object_id, "object": obj}

    async def read_mpsdk(self, data: MpsdkReadInput) -> dict[str, Any]:
        # CONTRACT: SyncService->ReadMpsdk->ReturnFunctionSource
        logger.debug(
            f"[SyncService:READ_MPSDK:ENTER] function_name={data.function_name}, "
            f"function_path={data.function_path}"
        )
        try:
            branch = daily_branch_name()
            content = await self._gitlab.get_file(data.function_path, ref=branch)
            ref = branch
            if content is None:
                base_branch = os.environ.get("MPSDK_BASE_BRANCH", "main")
                content = await self._gitlab.get_file(data.function_path, ref=base_branch)
                ref = base_branch
        except Exception as exc:
            raise SyncError(
                error="Failed to read from GitLab",
                apisix="untouched",
                gitlab="failed",
                detail=str(exc),
            ) from exc

        if content is None:
            logger.debug("[SyncService:READ_MPSDK:EXIT] file_exists=False")
            return {
                "found": False,
                "file_exists": False,
                "function_name": data.function_name,
                "function_path": data.function_path,
            }

        function_source = extract_function_source(content, data.function_name)
        logger.debug(
            f"[SyncService:READ_MPSDK:EXIT] found={function_source is not None}, ref={ref}"
        )
        return {
            "found": function_source is not None,
            "file_exists": True,
            "function_name": data.function_name,
            "function_path": data.function_path,
            "ref": ref,
            "source": function_source,
        }

    async def create_route(self, data: CreateRouteInput) -> dict[str, Any]:
        # CONTRACT: SyncService->CreateRoute->CreateApisixRouteAndMpsdkFunction
        route_id = route_id_from_uri(data.uri)
        logger.debug(f"[SyncService:CREATE_ROUTE:ENTER] route_id={route_id}")

        try:
            await self._apisix.get_route(route_id)
        except ApisixNotFoundError:
            pass
        except ApisixError as exc:
            raise SyncError(
                error="Failed to check route existence in APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc
        else:
            raise SyncError(
                error=f"Route already exists: {route_id}",
                apisix="untouched",
                gitlab="untouched",
                detail="create_route fails if the route already exists",
            )

        payload = build_route_payload(
            data.uri,
            data.anchor,
            data.method,
            data.service_id,
            data.rate_limit_count,
            data.rate_limit_period,
        )
        try:
            await self._apisix.put_route(route_id, payload)
        except ApisixError as exc:
            raise SyncError(
                error="Failed to create route in APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc
        logger.info(f"[SyncService:CREATE_ROUTE:STEP] apisix=applied, route_id={route_id}")

        try:
            gitlab_result = await self._gitlab_upsert_function(
                data.function_path, data.function_code, f"feat: add {data.uri}"
            )
        except Exception as exc:
            apisix_state = await self._compensate_create(route_id)
            raise SyncError(
                error="GitLab step failed after APISIX route creation",
                apisix=apisix_state,
                gitlab="failed",
                detail=str(exc),
            ) from exc

        logger.debug(f"[SyncService:CREATE_ROUTE:EXIT] route_id={route_id}")
        return {
            "status": "ok",
            "apisix": "applied",
            "gitlab": "applied",
            "route_id": route_id,
            **gitlab_result,
        }

    async def update_route(self, data: UpdateRouteInput) -> dict[str, Any]:
        # CONTRACT: SyncService->UpdateRoute->MergeAndWriteRouteAndFunction
        route_id = route_id_from_uri(data.uri)
        logger.debug(f"[SyncService:UPDATE_ROUTE:ENTER] route_id={route_id}")

        snapshot = await self._snapshot_route(route_id, "update")
        merged = dict(snapshot)
        if data.anchor is not None:
            merged["desc"] = data.anchor
        if data.method is not None:
            merged["methods"] = [data.method]
        if data.service_id is not None:
            merged["service_id"] = data.service_id
        if data.rate_limit_count is not None and data.rate_limit_period is not None:
            plugins = dict(merged.get("plugins") or {})
            plugins["limit-count"] = {
                "policy": "local",
                "count": data.rate_limit_count,
                "rejected_msg": f"rate-limit reached for {route_id}",
                "time_window": data.rate_limit_period,
                "rejected_code": 429,
            }
            merged["plugins"] = plugins

        try:
            await self._apisix.put_route(route_id, merged)
        except ApisixError as exc:
            raise SyncError(
                error="Failed to update route in APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc
        logger.info(f"[SyncService:UPDATE_ROUTE:STEP] apisix=applied, route_id={route_id}")

        try:
            gitlab_result = await self._gitlab_upsert_function(
                data.function_path, data.function_code, f"fix: update {data.uri}"
            )
        except Exception as exc:
            apisix_state = await self._compensate_restore(route_id, snapshot)
            raise SyncError(
                error="GitLab step failed after APISIX route update",
                apisix=apisix_state,
                gitlab="failed",
                detail=str(exc),
            ) from exc

        logger.debug(f"[SyncService:UPDATE_ROUTE:EXIT] route_id={route_id}")
        return {
            "status": "ok",
            "apisix": "applied",
            "gitlab": "applied",
            "route_id": route_id,
            **gitlab_result,
        }

    async def deprecate_route(self, data: DeprecateRouteInput) -> dict[str, Any]:
        # CONTRACT: SyncService->DeprecateRoute->TagRouteAndFunction
        route_id = route_id_from_uri(data.uri)
        logger.debug(f"[SyncService:DEPRECATE_ROUTE:ENTER] route_id={route_id}")

        snapshot = await self._snapshot_route(route_id, "deprecate")
        payload = dict(snapshot)
        name = str(snapshot.get("name") or route_id)
        if not name.startswith(DEPRECATED_PREFIX):
            name = f"{DEPRECATED_PREFIX}{name}"
        payload["name"] = name

        try:
            await self._apisix.put_route(route_id, payload)
        except ApisixError as exc:
            raise SyncError(
                error="Failed to deprecate route in APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc
        logger.info(f"[SyncService:DEPRECATE_ROUTE:STEP] apisix=applied, route_id={route_id}")

        message = data.message or f"Route {data.uri} is deprecated"
        try:
            gitlab_result = await self._gitlab_deprecate_function(
                data.function_path, data.function_name, message, f"deprecate: {data.uri}"
            )
        except Exception as exc:
            apisix_state = await self._compensate_restore(route_id, snapshot)
            raise SyncError(
                error="GitLab step failed after APISIX route deprecation",
                apisix=apisix_state,
                gitlab="failed",
                detail=str(exc),
            ) from exc

        logger.debug(f"[SyncService:DEPRECATE_ROUTE:EXIT] route_id={route_id}")
        return {
            "status": "ok",
            "apisix": "applied",
            "gitlab": "applied",
            "route_id": route_id,
            **gitlab_result,
        }

    async def delete_route(self, data: DeleteRouteInput) -> dict[str, Any]:
        # CONTRACT: SyncService->DeleteRoute->DisableRouteAndRecordEntry
        route_id = route_id_from_uri(data.uri)
        logger.debug(f"[SyncService:DELETE_ROUTE:ENTER] route_id={route_id}")

        snapshot = await self._snapshot_route(route_id, "delete")
        payload = dict(snapshot)
        payload["status"] = 0

        try:
            await self._apisix.put_route(route_id, payload)
        except ApisixError as exc:
            raise SyncError(
                error="Failed to disable route in APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc
        logger.info(f"[SyncService:DELETE_ROUTE:STEP] apisix=applied, route_id={route_id}")

        try:
            gitlab_result = await self._gitlab_mark_to_be_deleted(
                data.method,
                data.uri,
                data.function_name,
                data.function_path,
                f"chore: mark {data.uri} to-be-deleted",
            )
        except Exception as exc:
            apisix_state = await self._compensate_restore(route_id, snapshot)
            raise SyncError(
                error="GitLab step failed after APISIX route disabling",
                apisix=apisix_state,
                gitlab="failed",
                detail=str(exc),
            ) from exc

        logger.debug(f"[SyncService:DELETE_ROUTE:EXIT] route_id={route_id}")
        return {
            "status": "ok",
            "apisix": "applied",
            "gitlab": "applied",
            "route_id": route_id,
            **gitlab_result,
        }

    async def report_issue(self, data: ReportIssueInput) -> dict[str, Any]:
        # CONTRACT: SyncService->ReportIssue->CommitNeedsReviewEntry
        """Flag an unclear documentation fragment via NEEDS-REVIEW.md in a dedicated MR."""
        logger.debug(f"[SyncService:REPORT_ISSUE:ENTER] anchor={data.anchor}")
        try:
            branch = needs_review_branch_name()
            await self._gitlab.ensure_branch(branch)
            existing = await self._gitlab.get_file(NEEDS_REVIEW_PATH, ref=branch)
            new_content = append_needs_review(existing, needs_review_entry(data))

            if existing is not None and new_content == existing:
                logger.info(
                    f"[SyncService:REPORT_ISSUE:EXIT] anchor={data.anchor}, "
                    f"committed=False, reason=duplicate"
                )
                return {"status": "ok", "gitlab": "applied", "branch": branch, "committed": False}

            action = "create" if existing is None else "update"
            actions = [{"action": action, "file_path": NEEDS_REVIEW_PATH, "content": new_content}]
            commit = await self._gitlab.commit(
                branch, f"docs: flag {data.anchor} for review", actions
            )
            mr = await self._gitlab.ensure_merge_request(branch, title=needs_review_mr_title())
        except Exception as exc:
            raise SyncError(
                error="Failed to flag documentation fragment in GitLab",
                apisix="untouched",
                gitlab="failed",
                detail=str(exc),
            ) from exc

        logger.info(
            f"[SyncService:REPORT_ISSUE:EXIT] anchor={data.anchor}, committed=True, branch={branch}"
        )
        return {
            "status": "ok",
            "gitlab": "applied",
            "branch": branch,
            "committed": True,
            "commit_id": commit.get("id"),
            "merge_request": mr.get("web_url", mr.get("iid")),
        }

    async def _snapshot_route(self, route_id: str, operation: str) -> dict[str, Any]:
        # CONTRACT: SyncService->SnapshotRoute->GetExistingRoute
        try:
            return await self._apisix.get_route(route_id)
        except ApisixNotFoundError as exc:
            raise SyncError(
                error=f"Route does not exist: {route_id}",
                apisix="untouched",
                gitlab="untouched",
                detail=f"{operation}_route fails if the route does not exist",
            ) from exc
        except ApisixError as exc:
            raise SyncError(
                error="Failed to read route from APISIX",
                apisix="failed",
                gitlab="untouched",
                detail=str(exc),
            ) from exc

    async def _compensate_create(self, route_id: str) -> str:
        # CONTRACT: SyncService->CompensateCreate->DeleteCreatedRoute
        try:
            await self._apisix.delete_route(route_id)
            logger.info(f"[SyncService:COMPENSATE:STEP] apisix=reverted, route_id={route_id}")
            return "reverted"
        except Exception as exc:
            logger.error(
                f"[SyncService:COMPENSATE:ERROR] apisix=failed, route_id={route_id}, error={exc}"
            )
            return "failed"

    async def _compensate_restore(self, route_id: str, snapshot: dict[str, Any]) -> str:
        # CONTRACT: SyncService->CompensateRestore->PutSnapshotBack
        try:
            await self._apisix.put_route(route_id, snapshot)
            logger.info(f"[SyncService:COMPENSATE:STEP] apisix=reverted, route_id={route_id}")
            return "reverted"
        except Exception as exc:
            logger.error(
                f"[SyncService:COMPENSATE:ERROR] apisix=failed, route_id={route_id}, error={exc}"
            )
            return "failed"

    async def _commit_actions(
        self, branch: str, actions: list[dict[str, Any]], commit_message: str
    ) -> dict[str, Any]:
        # CONTRACT: SyncService->CommitActions->BumpVersionCommitEnsureMr
        pyproject = await self._gitlab.get_file(PYPROJECT_PATH, ref=branch)
        if pyproject is None:
            raise ValueError("pyproject.toml not found in MPSDK")
        bumped = bump_pyproject_patch(pyproject)
        if bumped != pyproject:
            actions.append({"action": "update", "file_path": PYPROJECT_PATH, "content": bumped})
        commit = await self._gitlab.commit(branch, commit_message, actions)
        mr = await self._gitlab.ensure_merge_request(branch)
        return {
            "branch": branch,
            "committed": True,
            "commit_id": commit.get("id"),
            "merge_request": mr.get("web_url", mr.get("iid")),
        }

    async def _gitlab_upsert_function(
        self, function_path: str, function_code: str, commit_message: str
    ) -> dict[str, Any]:
        # CONTRACT: SyncService->GitlabUpsertFunction->CommitFunctionChange
        logger.debug(f"[SyncService:GITLAB_UPSERT:ENTER] function_path={function_path}")
        branch = daily_branch_name()
        await self._gitlab.ensure_branch(branch)
        existing = await self._gitlab.get_file(function_path, ref=branch)
        new_source, function_name = upsert_function_source(existing, function_code)

        if existing is not None and new_source == existing:
            logger.info(
                f"[SyncService:GITLAB_UPSERT:EXIT] function_path={function_path}, "
                f"committed=False, reason=unchanged"
            )
            return {"branch": branch, "committed": False, "function_name": function_name}

        action = "create" if existing is None else "update"
        actions = [{"action": action, "file_path": function_path, "content": new_source}]
        result = await self._commit_actions(branch, actions, commit_message)
        logger.info(
            f"[SyncService:GITLAB_UPSERT:EXIT] function_path={function_path}, committed=True"
        )
        return {**result, "function_name": function_name}

    async def _gitlab_deprecate_function(
        self, function_path: str, function_name: str, message: str, commit_message: str
    ) -> dict[str, Any]:
        # CONTRACT: SyncService->GitlabDeprecateFunction->CommitDeprecation
        logger.debug(
            f"[SyncService:GITLAB_DEPRECATE:ENTER] function_path={function_path}, "
            f"function_name={function_name}"
        )
        branch = daily_branch_name()
        await self._gitlab.ensure_branch(branch)
        existing = await self._gitlab.get_file(function_path, ref=branch)
        if existing is None:
            raise ValueError(f"File not found in MPSDK: {function_path}")
        new_source = deprecate_function_source(existing, function_name, message)

        if new_source == existing:
            logger.info(
                f"[SyncService:GITLAB_DEPRECATE:EXIT] function_path={function_path}, "
                f"committed=False, reason=unchanged"
            )
            return {"branch": branch, "committed": False, "function_name": function_name}

        actions = [{"action": "update", "file_path": function_path, "content": new_source}]
        result = await self._commit_actions(branch, actions, commit_message)
        logger.info(
            f"[SyncService:GITLAB_DEPRECATE:EXIT] function_path={function_path}, committed=True"
        )
        return {**result, "function_name": function_name}

    async def _gitlab_mark_to_be_deleted(
        self,
        method: str,
        uri: str,
        function_name: str,
        function_path: str,
        commit_message: str,
    ) -> dict[str, Any]:
        # CONTRACT: SyncService->GitlabMarkToBeDeleted->CommitEntry
        logger.debug(f"[SyncService:GITLAB_MARK_DELETED:ENTER] uri={uri}")
        branch = daily_branch_name()
        await self._gitlab.ensure_branch(branch)
        existing = await self._gitlab.get_file(TO_BE_DELETED_PATH, ref=branch)
        new_content = append_to_be_deleted(existing, method, uri, function_name, function_path)

        if existing is not None and new_content == existing:
            logger.info(
                f"[SyncService:GITLAB_MARK_DELETED:EXIT] uri={uri}, "
                f"committed=False, reason=duplicate"
            )
            return {"branch": branch, "committed": False, "function_name": function_name}

        action = "create" if existing is None else "update"
        actions = [{"action": action, "file_path": TO_BE_DELETED_PATH, "content": new_content}]
        result = await self._commit_actions(branch, actions, commit_message)
        logger.info(f"[SyncService:GITLAB_MARK_DELETED:EXIT] uri={uri}, committed=True")
        return {**result, "function_name": function_name}


sync_service = SyncService()
