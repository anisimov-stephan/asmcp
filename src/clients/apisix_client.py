# src/clients/apisix_client.py
"""Async httpx client for the APISIX 3.x Admin API."""

import os
import re
from typing import Any

import httpx
from loguru import logger

_PARAM_PATTERN = re.compile(r"\{([^}/]+)\}")

KIND_PATHS = {
    "route": "routes",
    "service": "services",
    "upstream": "upstreams",
}


class ApisixError(Exception):
    """APISIX Admin API call failed."""


class ApisixNotFoundError(ApisixError):
    """Requested APISIX object does not exist."""


def route_id_from_uri(uri: str) -> str:
    # CONTRACT: ApisixClient->RouteIdFromUri->NormalizeIdentifier
    """Normalize a URI into a route id: strip leading slash, replace '/' with ':'."""
    return uri.lstrip("/").replace("/", ":")


def apisix_path_from_uri(uri: str) -> str:
    # CONTRACT: ApisixClient->ApisixPathFromUri->ConvertParamsToVariables
    """Convert '{param}' path parameters to APISIX ':param' path variables."""
    path = uri if uri.startswith("/") else f"/{uri}"
    return _PARAM_PATTERN.sub(r":\1", path)


class ApisixClient:
    """Client for the APISIX Admin API (routes, services, upstreams)."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        # CONTRACT: ApisixClient->Initialize->SetupClient
        logger.debug("[ApisixClient:INIT]")
        self._client = http_client

    def _config(self) -> tuple[str, str, float]:
        # CONTRACT: ApisixClient->LoadConfig->ReadEnv
        base_url = os.environ.get("APISIX_ADMIN_URL", "").rstrip("/")
        api_key = os.environ.get("APISIX_ADMIN_KEY", "")
        timeout = float(os.environ.get("EXTERNAL_HTTP_TIMEOUT_S", "30"))
        if not base_url:
            raise ApisixError("APISIX_ADMIN_URL is not configured")
        return base_url, api_key, timeout

    @staticmethod
    def _extract_value(data: Any) -> Any:
        # CONTRACT: ApisixClient->ExtractValue->UnwrapEtcdEnvelope
        if isinstance(data, dict) and isinstance(data.get("value"), dict):
            return data["value"]
        return data

    async def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        # CONTRACT: ApisixClient->Request->CallAdminApi
        base_url, api_key, timeout = self._config()
        url = f"{base_url}{path}"
        headers = {"X-API-KEY": api_key}
        logger.debug(f"[ApisixClient:REQUEST:ENTER] method={method}, url={url}")

        if self._client is not None:
            response = await self._client.request(method, url, json=json_body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method, url, json=json_body, headers=headers)

        if response.status_code == 404 and method == "GET":
            logger.debug(f"[ApisixClient:REQUEST:EXIT] status=404, url={url}")
            raise ApisixNotFoundError(f"APISIX object not found: {path}")
        if response.status_code >= 400:
            logger.error(f"[ApisixClient:REQUEST:ERROR] status={response.status_code}, url={url}")
            raise ApisixError(
                f"APISIX {method} {path} failed: {response.status_code} {response.text}"
            )

        logger.debug(f"[ApisixClient:REQUEST:EXIT] status={response.status_code}, url={url}")
        if not response.content:
            return {}
        return self._extract_value(response.json())

    async def read(self, kind: str, object_id: str) -> dict[str, Any]:
        # CONTRACT: ApisixClient->Read->GetObjectById
        """GET a route, service or upstream by id and return the raw APISIX object."""
        if kind not in KIND_PATHS:
            raise ApisixError(f"Unknown APISIX object kind: {kind}")
        logger.debug(f"[ApisixClient:READ:ENTER] kind={kind}, object_id={object_id}")
        result = await self._request("GET", f"/apisix/admin/{KIND_PATHS[kind]}/{object_id}")
        logger.debug(f"[ApisixClient:READ:EXIT] kind={kind}, object_id={object_id}")
        return result

    async def get_route(self, route_id: str) -> dict[str, Any]:
        # CONTRACT: ApisixClient->GetRoute->GetRouteById
        return await self.read("route", route_id)

    async def put_route(self, route_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # CONTRACT: ApisixClient->PutRoute->WriteRouteById
        """PUT the full route object (used for create, update, deprecate and disable)."""
        logger.debug(f"[ApisixClient:PUT_ROUTE:ENTER] route_id={route_id}")
        result = await self._request("PUT", f"/apisix/admin/routes/{route_id}", json_body=payload)
        logger.debug(f"[ApisixClient:PUT_ROUTE:EXIT] route_id={route_id}")
        return result

    async def delete_route(self, route_id: str) -> None:
        # CONTRACT: ApisixClient->DeleteRoute->RemoveRouteById
        """DELETE a route (used only as create_route compensation)."""
        logger.debug(f"[ApisixClient:DELETE_ROUTE:ENTER] route_id={route_id}")
        await self._request("DELETE", f"/apisix/admin/routes/{route_id}")
        logger.debug(f"[ApisixClient:DELETE_ROUTE:EXIT] route_id={route_id}")


apisix_client = ApisixClient()
