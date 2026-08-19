# src/clients/gitlab_client.py
"""Async httpx client for the GitLab REST API v4 (MPSDK project only)."""

import base64
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger


class GitLabError(Exception):
    """GitLab API call failed."""


def daily_branch_name() -> str:
    # CONTRACT: GitLabClient->DailyBranchName->ReturnUtcDateBranch
    """Return the daily update branch name: update-DD-MM-YYYY (UTC)."""
    return f"update-{datetime.now(UTC):%d-%m-%Y}"


def daily_mr_title() -> str:
    # CONTRACT: GitLabClient->DailyMrTitle->ReturnUtcDateTitle
    """Return the daily merge request title: MPSDK update DD-MM-YYYY (UTC)."""
    return f"MPSDK update {datetime.now(UTC):%d-%m-%Y}"


def needs_review_branch_name() -> str:
    # CONTRACT: GitLabClient->NeedsReviewBranchName->ReturnUtcDateBranch
    """Return the daily review-flag branch name: needs-review-DD-MM-YYYY (UTC)."""
    return f"needs-review-{datetime.now(UTC):%d-%m-%Y}"


def needs_review_mr_title() -> str:
    # CONTRACT: GitLabClient->NeedsReviewMrTitle->ReturnUtcDateTitle
    """Return the review-flag merge request title: MPSDK needs review DD-MM-YYYY (UTC)."""
    return f"MPSDK needs review {datetime.now(UTC):%d-%m-%Y}"


class GitLabClient:
    """Client for the GitLab REST API v4, restricted to the MPSDK project."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        # CONTRACT: GitLabClient->Initialize->SetupClient
        logger.debug("[GitLabClient:INIT]")
        self._client = http_client

    def _config(self) -> tuple[str, str, str, str, float]:
        # CONTRACT: GitLabClient->LoadConfig->ReadEnv
        base_url = os.environ.get("GITLAB_URL", "").rstrip("/")
        token = os.environ.get("GITLAB_TOKEN", "")
        project_id = os.environ.get("MPSDK_PROJECT_ID", "")
        base_branch = os.environ.get("MPSDK_BASE_BRANCH", "main")
        timeout = float(os.environ.get("EXTERNAL_HTTP_TIMEOUT_S", "30"))
        if not base_url or not project_id:
            raise GitLabError("GITLAB_URL and MPSDK_PROJECT_ID must be configured")
        return base_url, token, project_id, base_branch, timeout

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        # CONTRACT: GitLabClient->Request->CallGitLabApi
        base_url, token, project_id, _, timeout = self._config()
        url = f"{base_url}/api/v4/projects/{project_id}{path}"
        headers = {"PRIVATE-TOKEN": token}
        logger.debug(f"[GitLabClient:REQUEST:ENTER] method={method}, url={url}")

        if self._client is not None:
            response = await self._client.request(
                method, url, json=json_body, params=params, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method, url, json=json_body, params=params, headers=headers
                )

        if response.status_code >= 400:
            logger.error(f"[GitLabClient:REQUEST:ERROR] status={response.status_code}, url={url}")
            raise GitLabError(
                f"GitLab {method} {path} failed: {response.status_code} {response.text}"
            )

        logger.debug(f"[GitLabClient:REQUEST:EXIT] status={response.status_code}, url={url}")
        if not response.content:
            return {}
        return response.json()

    async def get_file(self, path: str, ref: str) -> str | None:
        # CONTRACT: GitLabClient->GetFile->ReturnContentOrNone
        """Get decoded file content at ref, or None when the file/ref does not exist."""
        logger.debug(f"[GitLabClient:GET_FILE:ENTER] path={path}, ref={ref}")
        base_url, token, project_id, _, timeout = self._config()
        url = f"{base_url}/api/v4/projects/{project_id}/repository/files/{quote(path, safe='')}"
        headers = {"PRIVATE-TOKEN": token}
        request_params = {"ref": ref}

        if self._client is not None:
            response = await self._client.get(url, params=request_params, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, params=request_params, headers=headers)

        if response.status_code == 404:
            logger.debug(f"[GitLabClient:GET_FILE:EXIT] path={path}, found=False")
            return None
        if response.status_code >= 400:
            logger.error(
                f"[GitLabClient:GET_FILE:ERROR] status={response.status_code}, path={path}"
            )
            raise GitLabError(
                f"GitLab GET file {path} failed: {response.status_code} {response.text}"
            )

        data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        logger.debug(f"[GitLabClient:GET_FILE:EXIT] path={path}, found=True")
        return content

    async def get_branch(self, branch: str) -> dict[str, Any] | None:
        # CONTRACT: GitLabClient->GetBranch->ReturnBranchOrNone
        logger.debug(f"[GitLabClient:GET_BRANCH:ENTER] branch={branch}")
        try:
            result = await self._request("GET", f"/repository/branches/{quote(branch, safe='')}")
        except GitLabError as exc:
            if "404" in str(exc):
                logger.debug(f"[GitLabClient:GET_BRANCH:EXIT] branch={branch}, found=False")
                return None
            raise
        logger.debug(f"[GitLabClient:GET_BRANCH:EXIT] branch={branch}, found=True")
        return result

    async def ensure_branch(self, branch: str) -> str:
        # CONTRACT: GitLabClient->EnsureBranch->CreateIfAbsent
        """Create the branch from MPSDK_BASE_BRANCH if absent, reuse if present."""
        logger.debug(f"[GitLabClient:ENSURE_BRANCH:ENTER] branch={branch}")
        if await self.get_branch(branch) is not None:
            logger.debug(f"[GitLabClient:ENSURE_BRANCH:EXIT] branch={branch}, created=False")
            return branch
        _, _, _, base_branch, _ = self._config()
        await self._request(
            "POST",
            "/repository/branches",
            json_body={"branch": branch, "ref": base_branch},
        )
        logger.info(f"[GitLabClient:ENSURE_BRANCH:EXIT] branch={branch}, created=True")
        return branch

    async def commit(
        self, branch: str, message: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # CONTRACT: GitLabClient->Commit->CreateCommitWithActions
        """Create a commit with the given file actions on the branch."""
        logger.debug(
            f"[GitLabClient:COMMIT:ENTER] branch={branch}, actions={len(actions)}, "
            f"message={message}"
        )
        result = await self._request(
            "POST",
            "/repository/commits",
            json_body={
                "branch": branch,
                "commit_message": message,
                "actions": actions,
            },
        )
        logger.info(f"[GitLabClient:COMMIT:EXIT] branch={branch}, commit_id={result.get('id')}")
        return result

    async def ensure_merge_request(self, branch: str, title: str | None = None) -> dict[str, Any]:
        # CONTRACT: GitLabClient->EnsureMergeRequest->ReuseOrCreate
        """Return the single open MR for the branch, creating it when absent."""
        logger.debug(f"[GitLabClient:ENSURE_MR:ENTER] branch={branch}")
        _, _, _, base_branch, _ = self._config()
        existing = await self._request(
            "GET",
            "/merge_requests",
            params={"source_branch": branch, "state": "opened"},
        )
        if existing:
            logger.debug(
                f"[GitLabClient:ENSURE_MR:EXIT] branch={branch}, created=False, "
                f"mr_iid={existing[0].get('iid')}"
            )
            return existing[0]
        result = await self._request(
            "POST",
            "/merge_requests",
            json_body={
                "source_branch": branch,
                "target_branch": base_branch,
                "title": title or daily_mr_title(),
            },
        )
        logger.info(
            f"[GitLabClient:ENSURE_MR:EXIT] branch={branch}, created=True, "
            f"mr_iid={result.get('iid')}"
        )
        return result


gitlab_client = GitLabClient()
