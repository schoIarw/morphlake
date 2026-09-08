"""Thin management-console client for the existing MorphLake API service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, BinaryIO

import httpx

from morphlake.config import Settings, get_settings


@dataclass(frozen=True)
class ApiResult:
    status_code: int
    payload: Any


class AdminApiClient:
    """Forward console operations without duplicating API business logic."""

    def __init__(self, settings: Settings):
        self.base_url = settings.api_base_url.rstrip("/")
        self.timeout = settings.admin_api_timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        file: tuple[str, BinaryIO, str] | None = None,
    ) -> ApiResult:
        files = {"file": file} if file else None
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                )
        except httpx.HTTPError as exc:
            return ApiResult(502, {"error": {"code": "api_unreachable", "message": str(exc)}})
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"message": response.text}
        return ApiResult(response.status_code, payload)

    def download(self, file_id: str, token: str) -> tuple[httpx.Response, httpx.Client]:
        return self.stream(f"/api/v1/files/{file_id}/download", token)

    def stream(self, path: str, token: str) -> tuple[httpx.Response, httpx.Client]:
        request = httpx.Request(
            "GET",
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        client = httpx.Client(timeout=None, trust_env=False)
        try:
            response = client.send(request, stream=True)
        except Exception:
            client.close()
            raise
        return response, client


def get_admin_api_client(
    settings: Settings = None,  # type: ignore[assignment]
) -> AdminApiClient:
    return AdminApiClient(settings or get_settings())
