from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from morphlake.config import Settings
from morphlake.main import create_app
from morphlake.services.extractors import classify


class FakeService:
    def initialize(self):
        return None

    def ready(self):
        return {"minio": "ok", "paimon": "ok"}

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

    def list_assets(self, **_):
        return []

    def full_text_search(self, _):
        return []

    def vector_search(self, _):
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


def test_modality_upload_contracts():
    cases = [
        ("documents", "a.txt", b"hello", "text/plain", "document"),
        ("images", "a.png", b"png", "image/png", "image"),
        ("audio", "a.mp3", b"mp3", "audio/mpeg", "audio"),
    ]
    for path, filename, body, content_type, expected in cases:
        response = client().post(
            f"/api/v1/files/{path}",
            data={"business_domain": "risk", "department": "audit"},
            files={"file": (filename, body, content_type)},
        )
        assert response.status_code == 201
        assert response.json()["media_type"] == expected


def test_query_file_vector_search_contract():
    response = client().post(
        "/api/v1/search/vector/file",
        data={
            "business_domain": "risk",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
        files={"file": ("query.png", b"png", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["returned"] == 1
    assert response.json()["items"][0]["rank"] == 1


def test_api_key_and_validation_error_contract():
    unauthorized = client("secret").get("/api/v1/files")
    assert unauthorized.status_code == 401
    invalid = client().post(
        "/api/v1/search/full-text",
        json={"business_domain": "risk", "keyword": ""},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
