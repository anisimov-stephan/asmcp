# tests/unit/test_sync_service.py
"""Unit tests for the sync service orchestration and MPSDK AST helpers."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.clients.apisix_client import ApisixError, ApisixNotFoundError
from src.clients.gitlab_client import GitLabError
from src.services.sync_service import (
    ApisixReadInput,
    CreateRouteInput,
    DeleteRouteInput,
    DeprecateRouteInput,
    MpsdkReadInput,
    ReportIssueInput,
    SyncError,
    SyncService,
    UpdateRouteInput,
    append_needs_review,
    append_to_be_deleted,
    build_route_payload,
    bump_pyproject_patch,
    deprecate_function_source,
    extract_function_source,
    needs_review_entry,
    to_be_deleted_entry,
    upsert_function_source,
)

PYPROJECT = '[project]\nname = "mpsdk"\nversion = "1.2.3"\n'
PYPROJECT_BUMPED = '[project]\nname = "mpsdk"\nversion = "1.2.4"\n'
FUNCTION_CODE = "def get_user(user_id: int) -> dict:\n    return {}\n"
EXISTING_FILE = "def other() -> None:\n    pass\n"
EXISTING_ROUTE = {
    "name": "users:{id}",
    "desc": "https://docs#users",
    "uri": "/users/:id",
    "methods": ["GET"],
    "service_id": "svc-1",
    "plugins": {
        "limit-count": {
            "policy": "local",
            "count": 10,
            "rejected_msg": "rate-limit reached for users:{id}",
            "time_window": 60,
            "rejected_code": 429,
        }
    },
}


def make_service() -> tuple[SyncService, AsyncMock, AsyncMock]:
    # CONTRACT: Fixture->MakeService->InjectMockClients
    apisix = AsyncMock()
    gitlab = AsyncMock()
    return SyncService(apisix=apisix, gitlab=gitlab), apisix, gitlab


def create_input(**overrides) -> CreateRouteInput:
    # CONTRACT: Fixture->CreateInput->DefaultPayload
    data = {
        "uri": "/users/{id}",
        "anchor": "https://docs#users",
        "method": "GET",
        "service_id": "svc-1",
        "function_code": FUNCTION_CODE,
        "function_path": "mpsdk/users.py",
    }
    data.update(overrides)
    return CreateRouteInput(**data)


def setup_gitlab_upsert(gitlab: AsyncMock, existing: str | None) -> None:
    # CONTRACT: Fixture->SetupGitlabUpsert->StubFileReads
    async def get_file(path: str, ref: str) -> str | None:
        if path == "pyproject.toml":
            return PYPROJECT
        return existing

    gitlab.get_file = AsyncMock(side_effect=get_file)
    gitlab.ensure_branch = AsyncMock()
    gitlab.commit = AsyncMock(return_value={"id": "c1"})
    gitlab.ensure_merge_request = AsyncMock(return_value={"iid": 7, "web_url": "mr-url"})


class TestUpsertFunctionSource:
    """Test AST-based function upserts."""

    def test_create_new_file(self):
        # CONTRACT: TestUpsert->NewFile->ReturnCode
        source, name = upsert_function_source(None, FUNCTION_CODE)
        assert name == "get_user"
        assert source == FUNCTION_CODE

    def test_append_to_existing_file(self):
        # CONTRACT: TestUpsert->Append->FunctionAdded
        source, name = upsert_function_source(EXISTING_FILE, FUNCTION_CODE)
        assert name == "get_user"
        assert "def other" in source
        assert "def get_user" in source

    def test_replace_existing_function(self):
        # CONTRACT: TestUpsert->Replace->FunctionReplaced
        old = "def get_user(user_id: int) -> dict:\n    return {'old': True}\n"
        source, name = upsert_function_source(old, FUNCTION_CODE)
        assert name == "get_user"
        assert "old" not in source
        assert source.count("def get_user") == 1

    def test_no_function_raises(self):
        # CONTRACT: TestUpsert->NoFunction->RaiseValueError
        with pytest.raises(ValueError, match="no function definition"):
            upsert_function_source(None, "x = 1\n")


class TestDeprecateFunctionSource:
    """Test @deprecated decorator insertion."""

    def test_adds_decorator_and_import(self):
        # CONTRACT: TestDeprecate->Add->DecoratorAndImport
        source = deprecate_function_source(EXISTING_FILE, "other", "gone")
        assert "from typing_extensions import deprecated" in source
        assert "@deprecated('gone')" in source
        assert source.index("from typing_extensions import deprecated") < source.index("def other")

    def test_idempotent(self):
        # CONTRACT: TestDeprecate->Idempotent->NoDuplicates
        once = deprecate_function_source(EXISTING_FILE, "other", "gone")
        twice = deprecate_function_source(once, "other", "gone")
        assert once == twice

    def test_missing_function_raises(self):
        # CONTRACT: TestDeprecate->Missing->RaiseValueError
        with pytest.raises(ValueError, match="not found"):
            deprecate_function_source(EXISTING_FILE, "missing", "gone")


class TestBumpPyprojectPatch:
    """Test patch version bump."""

    def test_bumps_patch(self):
        # CONTRACT: TestBump->Patch->Incremented
        assert bump_pyproject_patch(PYPROJECT) == PYPROJECT_BUMPED

    def test_missing_version_raises(self):
        # CONTRACT: TestBump->Missing->RaiseValueError
        with pytest.raises(ValueError, match="No version field"):
            bump_pyproject_patch("[project]\n")


class TestToBeDeleted:
    """Test TO-BE-DELETED.md entries."""

    def test_entry_format(self):
        # CONTRACT: TestToBeDeleted->Format->EntryLine
        marked = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = to_be_deleted_entry("GET", "/users/{id}", "get_user", "mpsdk/users.py")
        assert entry == f"- GET /users/{{id}} — get_user in mpsdk/users.py (marked {marked})"

    def test_append_to_missing_file(self):
        # CONTRACT: TestToBeDeleted->Create->EntryOnly
        content = append_to_be_deleted(None, "GET", "/x", "f", "a.py")
        assert content.startswith("- GET /x — f in a.py (marked ")

    def test_append_to_existing_file(self):
        # CONTRACT: TestToBeDeleted->Append->TwoEntries
        first = append_to_be_deleted(None, "GET", "/x", "f", "a.py")
        second = append_to_be_deleted(first, "POST", "/y", "g", "b.py")
        assert second.count("\n- ") == 1
        assert "POST /y" in second

    def test_duplicate_skipped(self):
        # CONTRACT: TestToBeDeleted->Duplicate->Unchanged
        first = append_to_be_deleted(None, "GET", "/x", "f", "a.py")
        assert append_to_be_deleted(first, "GET", "/x", "f", "a.py") == first


class TestExtractFunctionSource:
    """Test function extraction for mpsdk_read."""

    def test_found(self):
        # CONTRACT: TestExtract->Found->ReturnSource
        source = extract_function_source(EXISTING_FILE, "other")
        assert source is not None
        assert "def other" in source

    def test_not_found(self):
        # CONTRACT: TestExtract->Missing->ReturnNone
        assert extract_function_source(EXISTING_FILE, "missing") is None


class TestBuildRoutePayload:
    """Test APISIX route payload assembly."""

    def test_without_rate_limit(self):
        # CONTRACT: TestBuildPayload->NoRateLimit->NoPlugins
        payload = build_route_payload("/users/{id}", "anchor", "GET", "svc-1", None, None)
        assert payload == {
            "name": "users:{id}",
            "desc": "anchor",
            "uri": "/users/:id",
            "methods": ["GET"],
            "service_id": "svc-1",
        }

    def test_with_rate_limit(self):
        # CONTRACT: TestBuildPayload->RateLimit->LimitCountPlugin
        payload = build_route_payload("/users/{id}", "anchor", "GET", "svc-1", 100, 60)
        assert payload["plugins"]["limit-count"] == {
            "policy": "local",
            "count": 100,
            "rejected_msg": "rate-limit reached for users:{id}",
            "time_window": 60,
            "rejected_code": 429,
        }


class TestReadApisix:
    """Test apisix_read orchestration."""

    @pytest.mark.asyncio
    async def test_found(self):
        # CONTRACT: TestReadApisix->Found->ReturnObject
        service, apisix, _ = make_service()
        apisix.read = AsyncMock(return_value={"name": "r"})
        result = await service.read_apisix(ApisixReadInput(kind="route", object_id="r"))
        assert result["found"] is True
        assert result["object"] == {"name": "r"}

    @pytest.mark.asyncio
    async def test_not_found(self):
        # CONTRACT: TestReadApisix->NotFound->ReturnFoundFalse
        service, apisix, _ = make_service()
        apisix.read = AsyncMock(side_effect=ApisixNotFoundError("missing"))
        result = await service.read_apisix(ApisixReadInput(kind="route", object_id="r"))
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_error_raises_sync_error(self):
        # CONTRACT: TestReadApisix->Error->RaiseSyncError
        service, apisix, _ = make_service()
        apisix.read = AsyncMock(side_effect=ApisixError("500"))
        with pytest.raises(SyncError) as exc_info:
            await service.read_apisix(ApisixReadInput(kind="route", object_id="r"))
        assert exc_info.value.apisix == "failed"
        assert exc_info.value.gitlab == "untouched"


class TestReadMpsdk:
    """Test mpsdk_read orchestration."""

    @pytest.mark.asyncio
    async def test_found_on_daily_branch(self):
        # CONTRACT: TestReadMpsdk->DailyBranch->ReturnSource
        service, _, gitlab = make_service()
        gitlab.get_file = AsyncMock(return_value=EXISTING_FILE)
        result = await service.read_mpsdk(
            MpsdkReadInput(function_name="other", function_path="mpsdk/users.py")
        )
        assert result["found"] is True
        assert "def other" in result["source"]
        assert result["ref"].startswith("update-")

    @pytest.mark.asyncio
    async def test_fallback_to_base_branch(self):
        # CONTRACT: TestReadMpsdk->BaseFallback->ReturnSource
        service, _, gitlab = make_service()
        gitlab.get_file = AsyncMock(side_effect=[None, EXISTING_FILE])
        result = await service.read_mpsdk(
            MpsdkReadInput(function_name="other", function_path="mpsdk/users.py")
        )
        assert result["found"] is True
        assert result["ref"] == "main"

    @pytest.mark.asyncio
    async def test_file_missing(self):
        # CONTRACT: TestReadMpsdk->Missing->ReturnFoundFalse
        service, _, gitlab = make_service()
        gitlab.get_file = AsyncMock(return_value=None)
        result = await service.read_mpsdk(
            MpsdkReadInput(function_name="other", function_path="mpsdk/users.py")
        )
        assert result["found"] is False
        assert result["file_exists"] is False

    @pytest.mark.asyncio
    async def test_gitlab_error_raises_sync_error(self):
        # CONTRACT: TestReadMpsdk->Error->RaiseSyncError
        service, _, gitlab = make_service()
        gitlab.get_file = AsyncMock(side_effect=GitLabError("500"))
        with pytest.raises(SyncError) as exc_info:
            await service.read_mpsdk(
                MpsdkReadInput(function_name="other", function_path="mpsdk/users.py")
            )
        assert exc_info.value.apisix == "untouched"
        assert exc_info.value.gitlab == "failed"


class TestCreateRoute:
    """Test create_route orchestration."""

    @pytest.mark.asyncio
    async def test_happy_path(self):
        # CONTRACT: TestCreateRoute->HappyPath->RouteAndCommit
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))
        setup_gitlab_upsert(gitlab, existing=None)

        result = await service.create_route(
            create_input(rate_limit_count=100, rate_limit_period=60)
        )

        assert result["status"] == "ok"
        assert result["apisix"] == "applied"
        assert result["gitlab"] == "applied"
        assert result["route_id"] == "users:{id}"
        assert result["committed"] is True

        put_payload = apisix.put_route.call_args[0][1]
        assert put_payload["name"] == "users:{id}"
        assert put_payload["uri"] == "/users/:id"
        assert put_payload["plugins"]["limit-count"]["count"] == 100

        commit_body = gitlab.commit.call_args
        assert commit_body[0][1] == "feat: add /users/{id}"
        actions = commit_body[0][2]
        paths = {action["file_path"] for action in actions}
        assert paths == {"mpsdk/users.py", "pyproject.toml"}
        function_action = next(a for a in actions if a["file_path"] == "mpsdk/users.py")
        assert function_action["action"] == "create"
        bump_action = next(a for a in actions if a["file_path"] == "pyproject.toml")
        assert bump_action["content"] == PYPROJECT_BUMPED

    @pytest.mark.asyncio
    async def test_fails_if_route_exists(self):
        # CONTRACT: TestCreateRoute->Exists->UntouchedError
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))

        with pytest.raises(SyncError) as exc_info:
            await service.create_route(create_input())

        assert exc_info.value.apisix == "untouched"
        assert exc_info.value.gitlab == "untouched"
        apisix.put_route.assert_not_called()
        gitlab.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_apisix_put_failure(self):
        # CONTRACT: TestCreateRoute->PutFailure->FailedUntouched
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))
        apisix.put_route = AsyncMock(side_effect=ApisixError("400"))

        with pytest.raises(SyncError) as exc_info:
            await service.create_route(create_input())

        assert exc_info.value.apisix == "failed"
        assert exc_info.value.gitlab == "untouched"
        gitlab.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_gitlab_failure_compensates_with_delete(self):
        # CONTRACT: TestCreateRoute->GitlabFailure->DeleteRouteCompensation
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))
        gitlab.ensure_branch = AsyncMock(side_effect=GitLabError("boom"))

        with pytest.raises(SyncError) as exc_info:
            await service.create_route(create_input())

        assert exc_info.value.apisix == "reverted"
        assert exc_info.value.gitlab == "failed"
        assert "boom" in exc_info.value.detail
        apisix.delete_route.assert_called_once_with("users:{id}")

    @pytest.mark.asyncio
    async def test_compensation_failure_reported(self):
        # CONTRACT: TestCreateRoute->CompensationFailure->ApisixFailed
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))
        apisix.delete_route = AsyncMock(side_effect=ApisixError("500"))
        gitlab.ensure_branch = AsyncMock(side_effect=GitLabError("boom"))

        with pytest.raises(SyncError) as exc_info:
            await service.create_route(create_input())

        assert exc_info.value.apisix == "failed"
        assert exc_info.value.gitlab == "failed"

    @pytest.mark.asyncio
    async def test_idempotent_no_commit_when_unchanged(self):
        # CONTRACT: TestCreateRoute->Unchanged->SkipCommit
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))
        existing_source, _ = upsert_function_source(None, FUNCTION_CODE)
        setup_gitlab_upsert(gitlab, existing=existing_source)

        result = await service.create_route(create_input())

        assert result["committed"] is False
        gitlab.commit.assert_not_called()


class TestUpdateRoute:
    """Test update_route orchestration."""

    @pytest.mark.asyncio
    async def test_happy_path_merges_fields(self):
        # CONTRACT: TestUpdateRoute->HappyPath->MergeProvidedFields
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        setup_gitlab_upsert(gitlab, existing=EXISTING_FILE)

        data = UpdateRouteInput(
            uri="/users/{id}",
            anchor="https://docs#new",
            function_code=FUNCTION_CODE,
            function_path="mpsdk/users.py",
        )
        result = await service.update_route(data)

        assert result["status"] == "ok"
        put_payload = apisix.put_route.call_args[0][1]
        assert put_payload["desc"] == "https://docs#new"
        assert put_payload["methods"] == ["GET"]
        assert put_payload["plugins"] == EXISTING_ROUTE["plugins"]
        assert gitlab.commit.call_args[0][1] == "fix: update /users/{id}"

    @pytest.mark.asyncio
    async def test_rate_limit_updated_when_provided(self):
        # CONTRACT: TestUpdateRoute->RateLimit->PluginUpdated
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        setup_gitlab_upsert(gitlab, existing=EXISTING_FILE)

        data = UpdateRouteInput(
            uri="/users/{id}",
            function_code=FUNCTION_CODE,
            function_path="mpsdk/users.py",
            rate_limit_count=500,
            rate_limit_period=3600,
        )
        await service.update_route(data)

        put_payload = apisix.put_route.call_args[0][1]
        assert put_payload["plugins"]["limit-count"]["count"] == 500
        assert put_payload["plugins"]["limit-count"]["time_window"] == 3600

    @pytest.mark.asyncio
    async def test_fails_if_route_missing(self):
        # CONTRACT: TestUpdateRoute->Missing->UntouchedError
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))

        data = UpdateRouteInput(
            uri="/users/{id}",
            function_code=FUNCTION_CODE,
            function_path="mpsdk/users.py",
        )
        with pytest.raises(SyncError) as exc_info:
            await service.update_route(data)

        assert exc_info.value.apisix == "untouched"
        assert exc_info.value.gitlab == "untouched"
        apisix.put_route.assert_not_called()
        gitlab.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_gitlab_failure_restores_snapshot(self):
        # CONTRACT: TestUpdateRoute->GitlabFailure->SnapshotRestored
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        gitlab.ensure_branch = AsyncMock(side_effect=GitLabError("boom"))

        data = UpdateRouteInput(
            uri="/users/{id}",
            function_code=FUNCTION_CODE,
            function_path="mpsdk/users.py",
        )
        with pytest.raises(SyncError) as exc_info:
            await service.update_route(data)

        assert exc_info.value.apisix == "reverted"
        assert exc_info.value.gitlab == "failed"
        assert apisix.put_route.call_count == 2
        restore_payload = apisix.put_route.call_args_list[1][0][1]
        assert restore_payload == EXISTING_ROUTE


class TestDeprecateRoute:
    """Test deprecate_route orchestration."""

    @pytest.mark.asyncio
    async def test_happy_path(self):
        # CONTRACT: TestDeprecateRoute->HappyPath->NamePrefixedAndDecorated
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        setup_gitlab_upsert(gitlab, existing=EXISTING_FILE)

        data = DeprecateRouteInput(
            uri="/users/{id}",
            function_name="other",
            function_path="mpsdk/users.py",
            message="use v2",
        )
        result = await service.deprecate_route(data)

        assert result["status"] == "ok"
        put_payload = apisix.put_route.call_args[0][1]
        assert put_payload["name"] == "DEPRECATED: users:{id}"

        actions = gitlab.commit.call_args[0][2]
        function_action = next(a for a in actions if a["file_path"] == "mpsdk/users.py")
        assert "@deprecated('use v2')" in function_action["content"]
        assert "from typing_extensions import deprecated" in function_action["content"]
        assert gitlab.commit.call_args[0][1] == "deprecate: /users/{id}"

    @pytest.mark.asyncio
    async def test_already_deprecated_name_not_double_prefixed(self):
        # CONTRACT: TestDeprecateRoute->AlreadyDeprecated->SinglePrefix
        service, apisix, gitlab = make_service()
        route = dict(EXISTING_ROUTE)
        route["name"] = "DEPRECATED: users:{id}"
        apisix.get_route = AsyncMock(return_value=route)
        setup_gitlab_upsert(gitlab, existing=EXISTING_FILE)

        data = DeprecateRouteInput(
            uri="/users/{id}", function_name="other", function_path="mpsdk/users.py"
        )
        await service.deprecate_route(data)

        put_payload = apisix.put_route.call_args[0][1]
        assert put_payload["name"] == "DEPRECATED: users:{id}"

    @pytest.mark.asyncio
    async def test_fails_if_route_missing(self):
        # CONTRACT: TestDeprecateRoute->Missing->UntouchedError
        service, apisix, _ = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))

        data = DeprecateRouteInput(
            uri="/users/{id}", function_name="other", function_path="mpsdk/users.py"
        )
        with pytest.raises(SyncError) as exc_info:
            await service.deprecate_route(data)

        assert exc_info.value.apisix == "untouched"
        assert exc_info.value.gitlab == "untouched"

    @pytest.mark.asyncio
    async def test_gitlab_failure_restores_snapshot(self):
        # CONTRACT: TestDeprecateRoute->GitlabFailure->SnapshotRestored
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        gitlab.ensure_branch = AsyncMock(side_effect=GitLabError("boom"))

        data = DeprecateRouteInput(
            uri="/users/{id}", function_name="other", function_path="mpsdk/users.py"
        )
        with pytest.raises(SyncError) as exc_info:
            await service.deprecate_route(data)

        assert exc_info.value.apisix == "reverted"
        assert exc_info.value.gitlab == "failed"
        restore_payload = apisix.put_route.call_args_list[1][0][1]
        assert restore_payload == EXISTING_ROUTE


class TestDeleteRoute:
    """Test delete_route orchestration."""

    @pytest.mark.asyncio
    async def test_happy_path(self):
        # CONTRACT: TestDeleteRoute->HappyPath->DisabledAndRecorded
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        setup_gitlab_upsert(gitlab, existing=None)

        data = DeleteRouteInput(
            uri="/users/{id}",
            method="GET",
            function_name="get_user",
            function_path="mpsdk/users.py",
        )
        result = await service.delete_route(data)

        assert result["status"] == "ok"
        put_payload = apisix.put_route.call_args[0][1]
        assert put_payload["status"] == 0
        assert put_payload["name"] == EXISTING_ROUTE["name"]

        assert gitlab.commit.call_args[0][1] == "chore: mark /users/{id} to-be-deleted"
        actions = gitlab.commit.call_args[0][2]
        entry_action = next(a for a in actions if a["file_path"] == "TO-BE-DELETED.md")
        assert entry_action["action"] == "create"
        assert "- GET /users/{id} — get_user in mpsdk/users.py" in entry_action["content"]

    @pytest.mark.asyncio
    async def test_duplicate_entry_skips_commit(self):
        # CONTRACT: TestDeleteRoute->Duplicate->SkipCommit
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        existing = append_to_be_deleted(None, "GET", "/users/{id}", "get_user", "mpsdk/users.py")
        setup_gitlab_upsert(gitlab, existing=existing)

        data = DeleteRouteInput(
            uri="/users/{id}",
            method="GET",
            function_name="get_user",
            function_path="mpsdk/users.py",
        )
        result = await service.delete_route(data)

        assert result["committed"] is False
        gitlab.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_if_route_missing(self):
        # CONTRACT: TestDeleteRoute->Missing->UntouchedError
        service, apisix, _ = make_service()
        apisix.get_route = AsyncMock(side_effect=ApisixNotFoundError("missing"))

        data = DeleteRouteInput(
            uri="/users/{id}",
            method="GET",
            function_name="get_user",
            function_path="mpsdk/users.py",
        )
        with pytest.raises(SyncError) as exc_info:
            await service.delete_route(data)

        assert exc_info.value.apisix == "untouched"
        assert exc_info.value.gitlab == "untouched"

    @pytest.mark.asyncio
    async def test_gitlab_failure_restores_snapshot(self):
        # CONTRACT: TestDeleteRoute->GitlabFailure->SnapshotRestored
        service, apisix, gitlab = make_service()
        apisix.get_route = AsyncMock(return_value=dict(EXISTING_ROUTE))
        gitlab.ensure_branch = AsyncMock(side_effect=GitLabError("boom"))

        data = DeleteRouteInput(
            uri="/users/{id}",
            method="GET",
            function_name="get_user",
            function_path="mpsdk/users.py",
        )
        with pytest.raises(SyncError) as exc_info:
            await service.delete_route(data)

        assert exc_info.value.apisix == "reverted"
        assert exc_info.value.gitlab == "failed"
        restore_payload = apisix.put_route.call_args_list[1][0][1]
        assert restore_payload == EXISTING_ROUTE


def report_input(**overrides) -> ReportIssueInput:
    # CONTRACT: Fixture->ReportInput->DefaultPayload
    data = {
        "anchor": "https://docs#rate-limit",
        "fragment": "rate limit config section",
        "description": "Cannot tell which unit the period uses",
        "uri": "/users/{id}",
        "method": "GET",
    }
    data.update(overrides)
    return ReportIssueInput(**data)


class TestNeedsReviewEntry:
    """Test NEEDS-REVIEW.md entry formatting."""

    def test_entry_format(self):
        # CONTRACT: TestNeedsReview->Format->EntryBlock
        reported = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = needs_review_entry(report_input())
        assert f"## https://docs#rate-limit (reported {reported})" in entry
        assert "- URI: /users/{id} GET" in entry
        assert "- Fragment: rate limit config section" in entry
        assert "- Problem: Cannot tell which unit the period uses" in entry

    def test_entry_without_uri(self):
        # CONTRACT: TestNeedsReview->NoUri->DashPlaceholder
        entry = needs_review_entry(report_input(uri=None, method=None))
        assert "- URI: —" in entry

    def test_append_to_missing_file(self):
        # CONTRACT: TestNeedsReview->Create->EntryOnly
        entry = needs_review_entry(report_input())
        assert append_needs_review(None, entry) == entry

    def test_append_to_existing_file(self):
        # CONTRACT: TestNeedsReview->Append->TwoEntries
        first = append_needs_review(None, needs_review_entry(report_input()))
        second = append_needs_review(
            first, needs_review_entry(report_input(anchor="https://docs#other"))
        )
        assert "https://docs#rate-limit" in second
        assert "https://docs#other" in second

    def test_duplicate_skipped(self):
        # CONTRACT: TestNeedsReview->Duplicate->Unchanged
        entry = needs_review_entry(report_input())
        first = append_needs_review(None, entry)
        assert append_needs_review(first, entry) == first


class TestReportIssue:
    """Test report_issue orchestration."""

    @pytest.mark.asyncio
    async def test_happy_path(self):
        # CONTRACT: TestReportIssue->HappyPath->NeedsReviewCommit
        service, apisix, gitlab = make_service()
        gitlab.get_file = AsyncMock(return_value=None)
        gitlab.commit = AsyncMock(return_value={"id": "c1"})
        gitlab.ensure_merge_request = AsyncMock(return_value={"iid": 9, "web_url": "mr-url"})

        result = await service.report_issue(report_input())

        assert result["status"] == "ok"
        assert result["committed"] is True
        assert result["branch"].startswith("needs-review-")
        apisix.get_route.assert_not_called()

        commit_call = gitlab.commit.call_args
        assert commit_call[0][1] == "docs: flag https://docs#rate-limit for review"
        actions = commit_call[0][2]
        assert len(actions) == 1
        assert actions[0]["file_path"] == "NEEDS-REVIEW.md"
        assert actions[0]["action"] == "create"
        assert "Cannot tell which unit the period uses" in actions[0]["content"]

        mr_call = gitlab.ensure_merge_request.call_args
        assert mr_call.kwargs["title"].startswith("MPSDK needs review ")

    @pytest.mark.asyncio
    async def test_duplicate_entry_skips_commit(self):
        # CONTRACT: TestReportIssue->Duplicate->SkipCommit
        service, _, gitlab = make_service()
        existing = append_needs_review(None, needs_review_entry(report_input()))
        gitlab.get_file = AsyncMock(return_value=existing)

        result = await service.report_issue(report_input())

        assert result["committed"] is False
        gitlab.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_gitlab_failure_raises_sync_error(self):
        # CONTRACT: TestReportIssue->GitlabFailure->UntouchedFailed
        service, apisix, gitlab = make_service()
        gitlab.ensure_branch = AsyncMock(side_effect=GitLabError("boom"))

        with pytest.raises(SyncError) as exc_info:
            await service.report_issue(report_input())

        assert exc_info.value.apisix == "untouched"
        assert exc_info.value.gitlab == "failed"
        assert "boom" in exc_info.value.detail
        apisix.get_route.assert_not_called()
