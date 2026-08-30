from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from morphlake.config import Settings
from morphlake.main import create_app


class FakeService:
    def initialize(self):
        return None

    def ready(self):
        return {"minio": "ok", "paimon": "ok"}

    def upload(self, **values):
        return {
            "file_id": "file-1",
            "filename": values["filename"],
            "media_type": "document",
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

    def list_assets(self, **_):
        return []

    def full_text_search(self, _):
        return []

    def vector_search(self, _):
        return []


def client(api_key=None):
    settings = Settings(MORPHLAKE_API_KEY=api_key)
    return TestClient(create_app(settings, FakeService(), initialize=False))


def test_upload_contract():
    response = client().post(
        "/api/v1/files",
        data={"business_domain": "risk", "department": "audit"},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 201
    assert response.json()["file_id"] == "file-1"


def test_api_key_and_validation_error_contract():
    unauthorized = client("secret").get("/api/v1/files")
    assert unauthorized.status_code == 401
    invalid = client().post(
        "/api/v1/search/full-text",
        json={"business_domain": "risk", "keyword": ""},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
