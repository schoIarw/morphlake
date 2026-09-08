from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from morphlake.admin_store import AdminStore
from morphlake.config import Settings
from morphlake.main import create_app
from morphlake.services.extractors import classify


class FakeService:
    def __init__(self):
        self.list_filters = None
        self.full_text_scope = None
        self.vector_scope = None

    def initialize(self):
        return None

    def ready(self):
        return {"minio": "ok", "paimon": "ok", "models": "ok", "indexes": "ok"}

    def upload(self, **values):
        return {
            "file_id": "file-1",
            "filename": values["filename"],
            "media_type": classify(values["filename"]),
            "content_type": values["content_type"],
            "file_size": len(values["body"]),
            "business_domain": values["business_domain"],
            "department": values["department"],
            "created_at": datetime.now(UTC),
            "object_bucket": "data",
            "object_key": "risk/a.txt",
            "object_etag": "etag",
            "chunk_count": 1,
        }

    def list_assets(self, **filters):
        self.list_filters = filters
        return []

    def download(self, file_id):
        return (
            {
                "file_id": file_id,
                "filename": "a.txt",
                "media_type": "document",
                "content_type": "text/plain",
                "file_size": 5,
                "business_domain": "risk",
                "department": "audit",
                "object_etag": "etag",
            },
            iter([b"hello"]),
        )

    def full_text_search(self, request, business_domain, department):
        self.full_text_scope = (request, business_domain, department)
        return []

    def vector_search(self, request, business_domain, department):
        self.vector_scope = (request, business_domain, department)
        return []

    def vector_search_file(self, **_):
        return [
            {
                "file_id": "match-1",
                "filename": "similar.png",
                "media_type": "image",
                "business_domain": "risk",
                "department": "audit",
                "created_at": datetime.now(UTC),
                "record_type": "file",
                "chunk_index": None,
                "content_text": None,
            }
        ]


def build_client(
    tmp_path: Path,
    *,
    upload_requests: int = 20,
    download_requests: int = 20,
    expires_at: str | None = None,
):
    settings = Settings(
        MORPHLAKE_ADMIN_DB_PATH=tmp_path / "admin.db",
        MORPHLAKE_TOKEN_PEPPER="test-pepper",
        MORPHLAKE_METRICS_TOKEN="metrics-secret",
    )
    store = AdminStore(settings)
    store.initialize()
    created = store.create_token(
        business_domain="risk",
        department="audit",
        assignee_name="Alice",
        phone="13800000000",
        notes="test",
        allocated_by="pytest",
        expires_at=expires_at,
        period_seconds=60,
        upload_requests_limit=upload_requests,
        download_requests_limit=download_requests,
        upload_bytes_limit=10_000,
        download_bytes_limit=10_000,
    )
    service = FakeService()
    test_client = TestClient(
        create_app(settings, service, admin_store=store, initialize=False),
        headers={"Authorization": f"Bearer {created.plaintext}"},
    )
    test_client.app.state.fake_service = service
    return test_client, store, created


def test_all_business_routes_require_token(tmp_path: Path):
    test_client, _, _ = build_client(tmp_path)
    assert test_client.get("/admin").status_code == 404
    response = test_client.get("/api/v1/files", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_required"
    assert test_client.get("/health/live", headers={"Authorization": ""}).status_code == 200


def test_upload_contract_scope_and_audit(tmp_path: Path):
    test_client, store, _ = build_client(tmp_path)
    response = test_client.post(
        "/api/v1/files",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 201
    assert response.json()["file_id"] == "file-1"
    assert response.json()["business_domain"] == "risk"
    assert response.json()["department"] == "audit"
    event = store.recent_transfers()[0]
    assert event["operation"] == "upload"
    assert event["byte_count"] == 5
    assert event["status"] == "success"

    assert test_client.get("/api/v1/files").status_code == 200
    filters = test_client.app.state.fake_service.list_filters
    assert filters["business_domain"] == "risk"
    assert filters["department"] is None


def test_domain_and_admin_keys_receive_different_read_scopes(tmp_path: Path):
    test_client, store, _ = build_client(tmp_path)
    service = test_client.app.state.fake_service

    domain_search = test_client.post(
        "/api/v1/search/full-text", json={"keyword": "contract", "limit": 10}
    )
    assert domain_search.status_code == 200
    assert service.full_text_scope[1:] == ("risk", None)

    admin_key = next(
        row["plaintext"] for row in store.list_tokens(reveal=True) if row["access_level"] == "admin"
    )
    admin_headers = {"Authorization": f"Bearer {admin_key}"}
    assert test_client.get("/api/v1/files", headers=admin_headers).status_code == 200
    assert service.list_filters["business_domain"] is None
    assert service.list_filters["department"] is None
    admin_search = test_client.post(
        "/api/v1/search/vector",
        headers=admin_headers,
        json={"vector": [0.1], "vector_field": "text"},
    )
    assert admin_search.status_code == 200
    assert service.vector_scope[1:] == (None, None)


def test_domain_key_cannot_download_another_domain(tmp_path: Path):
    test_client, store, _ = build_client(tmp_path)
    finance = store.create_token(
        business_domain="finance",
        department="accounting",
        assignee_name="Bob",
        phone="13900000000",
        notes="test",
        allocated_by="pytest",
        period_seconds=60,
        upload_requests_limit=10,
        download_requests_limit=10,
        upload_bytes_limit=10_000,
        download_bytes_limit=10_000,
    )
    denied = test_client.get(
        "/api/v1/files/file-1/download",
        headers={"Authorization": f"Bearer {finance.plaintext}"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "token_scope_mismatch"


def test_modality_upload_contracts(tmp_path: Path):
    test_client, _, _ = build_client(tmp_path)
    cases = [
        ("documents", "a.txt", b"hello", "text/plain", "document"),
        ("images", "a.png", b"png", "image/png", "image"),
        ("audio", "a.mp3", b"mp3", "audio/mpeg", "audio"),
    ]
    for path, filename, body, content_type, expected in cases:
        response = test_client.post(
            f"/api/v1/files/{path}",
            files={"file": (filename, body, content_type)},
        )
        assert response.status_code == 201
        assert response.json()["media_type"] == expected


def test_query_file_vector_search_contract(tmp_path: Path):
    test_client, _, _ = build_client(tmp_path)
    response = test_client.post(
        "/api/v1/search/vector/file",
        data={
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
        files={"file": ("query.png", b"png", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["returned"] == 1
    assert response.json()["items"][0]["rank"] == 1


def test_openapi_contract_does_not_expose_scope_fields(tmp_path: Path):
    test_client, _, _ = build_client(tmp_path)
    schema = test_client.get(
        "/openapi.json",
        auth=("admin", "change-me"),
        headers={"Authorization": ""},
    ).json()

    def resolve(request_schema):
        reference = request_schema.get("$ref")
        if not reference:
            return request_schema
        return schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]

    for path in (
        "/api/v1/files",
        "/api/v1/files/documents",
        "/api/v1/files/images",
        "/api/v1/files/audio",
        "/api/v1/search/vector/file",
    ):
        body = schema["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"][
            "schema"
        ]
        properties = resolve(body).get("properties", {})
        assert "business_domain" not in properties
        assert "department" not in properties

    list_parameters = {
        parameter["name"] for parameter in schema["paths"]["/api/v1/files"]["get"]["parameters"]
    }
    assert "business_domain" not in list_parameters
    assert "department" not in list_parameters

    for component in ("FullTextSearchRequest", "VectorSearchRequest"):
        properties = schema["components"]["schemas"][component]["properties"]
        assert "business_domain" not in properties
        assert "department" not in properties


def test_token_state_errors_are_distinct(tmp_path: Path):
    test_client, store, created = build_client(tmp_path)
    store.set_token_status(created.identity.token_id, "disabled")
    disabled = test_client.get("/api/v1/files")
    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "token_disabled"

    store.set_token_status(created.identity.token_id, "deleted")
    deleted = test_client.get("/api/v1/files")
    assert deleted.status_code == 403
    assert deleted.json()["error"]["code"] == "token_deleted"

    expired_client, _, _ = build_client(
        tmp_path / "expired",
        expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    expired = expired_client.get("/api/v1/files")
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "token_expired"


def test_upload_rate_limit_and_retry_after(tmp_path: Path):
    test_client, store, _ = build_client(tmp_path, upload_requests=1)
    values = {
        "files": {"file": ("a.txt", b"hello", "text/plain")},
    }
    assert test_client.post("/api/v1/files", **values).status_code == 201
    limited = test_client.post("/api/v1/files", **values)
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert store.recent_transfers()[0]["status"] == "rejected"


def test_download_metrics_and_validation(tmp_path: Path):
    test_client, store, _ = build_client(tmp_path)
    download = test_client.get("/api/v1/files/file-1/download")
    assert download.status_code == 200
    assert download.content == b"hello"
    assert store.recent_transfers()[0]["operation"] == "download"

    metrics = test_client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )
    assert metrics.status_code == 200
    assert "morphlake_transfer_bytes_total" in metrics.text

    invalid = test_client.post(
        "/api/v1/search/full-text",
        json={"keyword": ""},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
