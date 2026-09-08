from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from morphlake.admin_api_client import ApiResult
from morphlake.admin_main import create_admin_app
from morphlake.admin_store import AdminStore
from morphlake.config import Settings


class FakeApiClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, token: str, **kwargs: Any) -> ApiResult:
        self.calls.append({"method": method, "path": path, "token": token, **kwargs})
        if path == "/health/ready":
            return ApiResult(200, {"status": "ok", "checks": {"minio": "ok"}})
        if path.endswith("/preview"):
            return ApiResult(
                200,
                {
                    "file_id": "file-1",
                    "filename": "report.pdf",
                    "media_type": "document",
                    "content_type": "application/pdf",
                    "summary_text": "report summary",
                    "content_text": "report body",
                },
            )
        if path in {"/api/v1/files", "/api/v1/admin/files"} and method == "GET":
            return ApiResult(
                200,
                {
                    "items": [
                        {
                            "file_id": "file-1",
                            "filename": "report.pdf",
                            "media_type": "document",
                            "file_size": 2048,
                            "summary_text": "report summary",
                            "embedding_preview": [0.1, 0.2, 0.3],
                            "embedding_dimension": 768,
                        }
                    ],
                    "returned": 1,
                    "total": 41,
                },
            )
        if "search" in path:
            return ApiResult(
                200,
                {"items": [{"rank": 1, "file_id": "file-1"}], "returned": 1},
            )
        return ApiResult(201, {"file_id": "file-1", "filename": "report.pdf"})

    def download(self, file_id: str, token: str):
        self.calls.append(
            {"method": "GET", "path": f"/api/v1/files/{file_id}/download", "token": token}
        )
        request = httpx.Request("GET", "http://api.test/download")
        response = httpx.Response(
            200,
            stream=httpx.ByteStream(b"download-body"),
            headers={"Content-Type": "application/pdf", "Content-Disposition": "attachment"},
            request=request,
        )
        return response, DummyCloser()

    def stream(self, path: str, token: str):
        self.calls.append({"method": "GET", "path": path, "token": token})
        content_type = "image/jpeg" if path.endswith("thumbnail") else "audio/mpeg"
        request = httpx.Request("GET", f"http://api.test{path}")
        response = httpx.Response(
            200,
            stream=httpx.ByteStream(b"stream-body"),
            headers={"Content-Type": content_type},
            request=request,
        )
        return response, DummyCloser()


class DummyCloser:
    def close(self) -> None:
        pass


def build_admin(tmp_path: Path):
    settings = Settings(
        MORPHLAKE_ADMIN_DB_PATH=tmp_path / "admin.db",
        MORPHLAKE_ADMIN_USERNAME="root",
        MORPHLAKE_ADMIN_PASSWORD="safe-password",
        MORPHLAKE_TOKEN_PEPPER="test-pepper",
        MORPHLAKE_ADMIN_SESSION_SECRET="test-session-secret",
        MORPHLAKE_METRICS_TOKEN="metrics-secret",
        MORPHLAKE_API_BASE_URL="http://api.test",
    )
    store = AdminStore(settings)
    api_client = FakeApiClient()
    client = TestClient(create_admin_app(settings, store, api_client=api_client))
    return client, store, api_client


def login(client: TestClient) -> str:
    response = client.post(
        "/admin/login",
        data={"username": "root", "password": "safe-password", "next": "/admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/admin/tokens")
    assert page.status_code == 200
    return page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_admin_login_session_layout_and_token_lifecycle(tmp_path: Path):
    client, store, _ = build_admin(tmp_path)
    unauthenticated = client.get("/admin", follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"].startswith("/admin/login")
    login_page = client.get("/admin/login")
    assert "登录管理系统" in login_page.text
    invalid = client.post("/admin/login", data={"username": "root", "password": "wrong"})
    assert invalid.status_code == 401

    csrf = login(client)
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert '<header class="topbar">' in dashboard.text
    assert '<aside class="sidebar">' in dashboard.text
    assert '<main class="content">' in dashboard.text
    assert "文件上传" in dashboard.text
    assert "向量检索" in dashboard.text
    keys_page = client.get("/admin/tokens")
    assert "全域管理" in keys_page.text
    assert "key-copy" in keys_page.text

    created = client.post(
        "/admin/tokens",
        data={
            "csrf": csrf,
            "business_domain": "risk",
            "department": "audit",
            "assignee_name": "Alice",
            "phone": "13800000000",
            "notes": "mobile app",
            "period_seconds": "60",
            "upload_requests_limit": "10",
            "download_requests_limit": "20",
            "upload_bytes_limit": "1000",
            "download_bytes_limit": "2000",
            "expires_at": "",
        },
    )
    assert created.status_code == 201
    assert "mlk_" in created.text
    row = store.list_tokens()[0]
    assert row["assignee_name"] == "Alice"
    token_id = row["token_id"]
    plaintext = next(
        item["plaintext"] for item in store.list_tokens(reveal=True) if item["token_id"] == token_id
    )
    visible = client.get("/admin/tokens")
    assert plaintext in visible.text
    assert "复制" in visible.text
    limits = client.post(
        f"/admin/tokens/{token_id}/limits",
        data={
            "csrf": csrf,
            "period_seconds": "120",
            "upload_requests_limit": "11",
            "download_requests_limit": "22",
            "upload_bytes_limit": "1100",
            "download_bytes_limit": "2200",
        },
        follow_redirects=False,
    )
    assert limits.status_code == 303
    assert store.list_tokens()[0]["period_seconds"] == 120
    assert (
        client.post(
            f"/admin/tokens/{token_id}/status/disable",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert store.list_tokens()[0]["status"] == "disabled"
    rotated = client.post(
        f"/admin/tokens/{token_id}/rotate",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert rotated.status_code == 303
    new_plaintext = next(
        item["plaintext"] for item in store.list_tokens(reveal=True) if item["token_id"] == token_id
    )
    assert new_plaintext != plaintext

    logout = client.post("/admin/logout", data={"csrf": csrf}, follow_redirects=False)
    assert logout.status_code == 303
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_all_api_console_pages_forward_to_existing_api(tmp_path: Path):
    client, _, api = build_admin(tmp_path)
    csrf = login(client)
    token = "mlk_secret_business_token"

    for path in (
        "/admin/api/upload",
        "/admin/api/files",
        "/admin/api/full-text",
        "/admin/api/vector",
        "/admin/api/download",
        "/admin/api/status",
    ):
        response = client.get(path)
        assert response.status_code == 200
        if path == "/admin/api/files":
            assert "共 41 条" in response.text
            assert "业务域（可选）" in response.text
            assert "描述/概要模糊匹配" in response.text
            assert "下一页" in response.text
        else:
            assert "API Key" in response.text
            assert 'name="business_domain"' not in response.text
            assert 'name="department"' not in response.text
        assert "spinner" in response.text
        assert "form.dataset.submitting" in response.text

    upload = client.post(
        "/admin/api/upload",
        data={
            "csrf": csrf,
            "api_token": token,
            "mode": "document",
        },
        files={"file": ("report.pdf", b"pdf", "application/pdf")},
    )
    assert upload.status_code == 201
    assert api.calls[-1]["path"] == "/api/v1/files/documents"
    assert token not in upload.text

    files = client.post(
        "/admin/api/files",
        data={
            "csrf": csrf,
            "api_token": token,
            "media_type": "document",
            "filename": "report",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "limit": "50",
            "offset": "0",
        },
    )
    assert files.status_code == 200
    assert "report.pdf" in files.text
    assert "report summary" in files.text
    assert "[0.1, 0.2, 0.3, …]" in files.text
    assert "preview-open" in files.text
    assert "download-file" in files.text
    assert api.calls[-1]["path"] == "/api/v1/admin/files"

    full_text = client.post(
        "/admin/api/full-text",
        data={
            "csrf": csrf,
            "api_token": token,
            "keyword": "contract",
            "start_date": "",
            "end_date": "",
            "limit": "20",
        },
    )
    assert full_text.status_code == 200
    assert api.calls[-1]["path"] == "/api/v1/search/full-text"

    vector = client.post(
        "/admin/api/vector",
        data={
            "csrf": csrf,
            "api_token": token,
            "vector": "0.1, 0.2 0.3",
            "vector_field": "text",
            "start_date": "",
            "end_date": "",
            "limit": "10",
        },
    )
    assert vector.status_code == 200
    assert api.calls[-1]["json_body"]["vector"] == [0.1, 0.2, 0.3]

    vector_file = client.post(
        "/admin/api/vector/file",
        data={
            "csrf": csrf,
            "api_token": token,
            "start_date": "",
            "end_date": "",
        },
        files={"file": ("query.png", b"png", "image/png")},
    )
    assert vector_file.status_code == 200
    assert api.calls[-1]["path"] == "/api/v1/search/vector/file"

    ready = client.post("/admin/api/status", data={"csrf": csrf, "api_token": token})
    assert ready.status_code == 200
    assert api.calls[-1]["path"] == "/health/ready"

    download = client.post(
        "/admin/api/download",
        data={"csrf": csrf, "api_token": token, "file_id": "file-1"},
    )
    assert download.status_code == 200
    assert download.content == b"download-body"
    assert api.calls[-1]["path"] == "/api/v1/files/file-1/download"

    preview = client.get("/admin/api/files/file-1/preview", headers={"X-MorphLake-Key": token})
    assert preview.status_code == 200
    assert preview.json()["summary_text"] == "report summary"
    assert api.calls[-1]["token"] == token

    thumbnail = client.get("/admin/api/files/file-1/thumbnail", headers={"X-MorphLake-Key": token})
    assert thumbnail.status_code == 200
    assert thumbnail.content == b"stream-body"

    media = client.get("/admin/api/files/file-1/media", headers={"X-MorphLake-Key": token})
    assert media.status_code == 200
    assert media.headers["content-type"] == "audio/mpeg"

    filtered = client.get(
        "/admin/api/files",
        params={
            "business_domain": "risk",
            "department": "audit",
            "filename": "report",
            "description": "summary",
            "page": 3,
            "page_size": 20,
        },
    )
    assert filtered.status_code == 200
    assert "risk" in filtered.text
    assert "共 41 条" in filtered.text
    assert api.calls[-1]["path"] == "/api/v1/admin/files"
    assert api.calls[-1]["params"]["offset"] == 40
    assert api.calls[-1]["params"]["description"] == "summary"

    default_preview = client.get("/admin/api/files/file-1/preview")
    assert default_preview.status_code == 200
    assert api.calls[-1]["token"].startswith("mlk_")


def test_admin_is_independent_and_exports_metrics(tmp_path: Path):
    client, _, _ = build_admin(tmp_path)
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/v1/files").status_code == 404
    assert client.get("/metrics").status_code == 401
    metrics = client.get("/metrics", headers={"X-Metrics-Token": "metrics-secret"})
    assert metrics.status_code == 200
    assert 'morphlake_component_up{component="management_db"} 1.0' in metrics.text
    assert 'morphlake_management_db_info{backend="sqlite"} 1.0' in metrics.text
    assert metrics.headers["x-content-type-options"] == "nosniff"
