from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from morphlake.admin_main import create_admin_app
from morphlake.admin_store import AdminStore
from morphlake.config import Settings


def build_admin(tmp_path: Path):
    settings = Settings(
        MORPHLAKE_ADMIN_DB_PATH=tmp_path / "admin.db",
        MORPHLAKE_ADMIN_USERNAME="root",
        MORPHLAKE_ADMIN_PASSWORD="safe-password",
        MORPHLAKE_TOKEN_PEPPER="test-pepper",
        MORPHLAKE_METRICS_TOKEN="metrics-secret",
    )
    store = AdminStore(settings)
    client = TestClient(create_admin_app(settings, store))
    return client, store


def test_admin_requires_basic_auth_and_creates_token(tmp_path: Path):
    client, store = build_admin(tmp_path)
    assert client.get("/admin").status_code == 401
    auth = ("root", "safe-password")
    page = client.get("/admin/tokens", auth=auth)
    assert page.status_code == 200
    csrf = page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    created = client.post(
        "/admin/tokens",
        auth=auth,
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
    limits = client.post(
        f"/admin/tokens/{token_id}/limits",
        auth=auth,
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
    disabled = client.post(
        f"/admin/tokens/{token_id}/status/disable",
        auth=auth,
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    assert store.list_tokens()[0]["status"] == "disabled"


def test_admin_is_independent_and_exports_metrics(tmp_path: Path):
    client, _ = build_admin(tmp_path)
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/v1/files").status_code == 404
    denied = client.get("/metrics")
    assert denied.status_code == 401
    metrics = client.get("/metrics", headers={"X-Metrics-Token": "metrics-secret"})
    assert metrics.status_code == 200
    assert 'morphlake_component_up{component="sqlite"} 1.0' in metrics.text
