# tests/unit/test_gitlab_client.py
"""Unit tests for the GitLab REST API v4 client."""

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from src.clients.gitlab_client import GitLabClient, GitLabError, daily_branch_name, daily_mr_title


@pytest.fixture
def mock_env(monkeypatch):
    # CONTRACT: Fixture->MockEnv->SetGitLabEnvVars
    monkeypatch.setenv("GITLAB_URL", "http://gitlab.test")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    monkeypatch.setenv("MPSDK_PROJECT_ID", "42")
    monkeypatch.setenv("MPSDK_BASE_BRANCH", "main")
    monkeypatch.setenv("EXTERNAL_HTTP_TIMEOUT_S", "30")


def make_client(handler) -> GitLabClient:
    # CONTRACT: Fixture->MakeClient->InjectMockTransport
    transport = httpx.MockTransport(handler)
    return GitLabClient(http_client=httpx.AsyncClient(transport=transport))


def file_response(content: str) -> httpx.Response:
    # CONTRACT: Fixture->FileResponse->EncodeBase64
    encoded = base64.b64encode(content.encode()).decode()
    return httpx.Response(200, json={"content": encoded, "encoding": "base64"})


class TestDailyNames:
    """Test daily branch and MR naming."""

    def test_daily_branch_name(self):
        # CONTRACT: TestDailyNames->Branch->UpdateDdMmYyyy
        expected = datetime.now(UTC).strftime("update-%d-%m-%Y")
        assert daily_branch_name() == expected

    def test_daily_mr_title(self):
        # CONTRACT: TestDailyNames->MrTitle->MpsdkUpdateDdMmYyyy
        expected = datetime.now(UTC).strftime("MPSDK update %d-%m-%Y")
        assert daily_mr_title() == expected


class TestGetFile:
    """Test file reads."""

    @pytest.mark.asyncio
    async def test_get_file_decodes_base64(self, mock_env):
        # CONTRACT: TestGetFile->Decode->ReturnContent
        def handler(request: httpx.Request) -> httpx.Response:
            assert b"/api/v4/projects/42/repository/files/mpsdk%2Fclient.py" in (
                request.url.raw_path
            )
            assert request.url.params["ref"] == "main"
            assert request.headers["PRIVATE-TOKEN"] == "test-token"
            return file_response("def f():\n    pass\n")

        client = make_client(handler)
        content = await client.get_file("mpsdk/client.py", ref="main")
        assert content == "def f():\n    pass\n"

    @pytest.mark.asyncio
    async def test_get_file_not_found_returns_none(self, mock_env):
        # CONTRACT: TestGetFile->NotFound->ReturnNone
        client = make_client(lambda request: httpx.Response(404, json={"message": "404"}))
        assert await client.get_file("missing.py", ref="main") is None

    @pytest.mark.asyncio
    async def test_get_file_error_raises(self, mock_env):
        # CONTRACT: TestGetFile->Error->RaiseGitLabError
        client = make_client(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(GitLabError, match="500"):
            await client.get_file("x.py", ref="main")


class TestEnsureBranch:
    """Test branch reuse/creation."""

    @pytest.mark.asyncio
    async def test_existing_branch_reused(self, mock_env):
        # CONTRACT: TestEnsureBranch->Exists->Reuse
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={"name": "update-01-01-2026"})

        client = make_client(handler)
        branch = await client.ensure_branch("update-01-01-2026")
        assert branch == "update-01-01-2026"
        assert calls == [("GET", "/api/v4/projects/42/repository/branches/update-01-01-2026")]

    @pytest.mark.asyncio
    async def test_missing_branch_created_from_base(self, mock_env):
        # CONTRACT: TestEnsureBranch->Missing->CreateFromBase
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(404, json={"message": "404 Branch Not Found"})
            body = json.loads(request.content)
            assert body == {"branch": "update-01-01-2026", "ref": "main"}
            return httpx.Response(201, json={"name": "update-01-01-2026"})

        client = make_client(handler)
        branch = await client.ensure_branch("update-01-01-2026")
        assert branch == "update-01-01-2026"
        assert calls == [
            ("GET", "/api/v4/projects/42/repository/branches/update-01-01-2026"),
            ("POST", "/api/v4/projects/42/repository/branches"),
        ]


class TestCommit:
    """Test commit creation."""

    @pytest.mark.asyncio
    async def test_commit_sends_actions(self, mock_env):
        # CONTRACT: TestCommit->Actions->PostCommit
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "abc123"})

        client = make_client(handler)
        actions = [{"action": "update", "file_path": "a.py", "content": "x"}]
        result = await client.commit("update-01-01-2026", "feat: add /x", actions)
        assert captured["path"] == "/api/v4/projects/42/repository/commits"
        assert captured["body"] == {
            "branch": "update-01-01-2026",
            "commit_message": "feat: add /x",
            "actions": actions,
        }
        assert result["id"] == "abc123"

    @pytest.mark.asyncio
    async def test_commit_error_raises(self, mock_env):
        # CONTRACT: TestCommit->Error->RaiseGitLabError
        client = make_client(lambda request: httpx.Response(400, text="bad"))
        with pytest.raises(GitLabError, match="400"):
            await client.commit("b", "m", [])


class TestEnsureMergeRequest:
    """Test merge request reuse/creation."""

    @pytest.mark.asyncio
    async def test_existing_mr_reused(self, mock_env):
        # CONTRACT: TestEnsureMr->Exists->Reuse
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.params["source_branch"] == "update-01-01-2026"
            assert request.url.params["state"] == "opened"
            return httpx.Response(200, json=[{"iid": 7, "web_url": "http://gitlab.test/mr/7"}])

        client = make_client(handler)
        mr = await client.ensure_merge_request("update-01-01-2026")
        assert mr["iid"] == 7

    @pytest.mark.asyncio
    async def test_missing_mr_created(self, mock_env):
        # CONTRACT: TestEnsureMr->Missing->Create
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[])
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"iid": 8})

        client = make_client(handler)
        mr = await client.ensure_merge_request("update-01-01-2026")
        assert mr["iid"] == 8
        assert captured["body"]["source_branch"] == "update-01-01-2026"
        assert captured["body"]["target_branch"] == "main"
        assert captured["body"]["title"].startswith("MPSDK update ")


class TestConfig:
    """Test client configuration."""

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, monkeypatch):
        # CONTRACT: TestConfig->Missing->RaiseGitLabError
        monkeypatch.delenv("GITLAB_URL", raising=False)
        monkeypatch.delenv("MPSDK_PROJECT_ID", raising=False)
        client = make_client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(GitLabError, match="GITLAB_URL"):
            await client.get_branch("b")
