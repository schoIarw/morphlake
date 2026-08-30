from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from morphlake.config import Settings
from morphlake.errors import MorphLakeError
from morphlake.services.embeddings import ModelGateway
from morphlake.services.morphlake import MorphLakeService


class FakeObjects:
    def __init__(self):
        self.deleted = []
        self.values = {}

    def ensure_buckets(self):
        return None

    def put(self, key, body, content_type):
        self.values[key] = body
        return SimpleNamespace(bucket="data", key=key, etag="etag", size=len(body))

    def delete(self, key):
        self.deleted.append(key)

    def stream(self, key):
        yield self.values[key]

    def ping(self):
        return None


class FakeCatalog:
    def __init__(self, fail=False):
        self.fail = fail
        self.records = []

    def initialize(self):
        return None

    def add(self, records):
        if self.fail:
            raise RuntimeError("commit failed")
        self.records.extend(records)

    def ping(self):
        return None


def models(tmp_path: Path) -> ModelGateway:
    path = tmp_path / "models.yaml"
    path.write_text(
        """models:
  text_embedding: {provider: hash, model: text, dimension: 4}
  image_embedding: {provider: hash, model: image, dimension: 5}
  audio_transcription: {provider: none, model: none}
  audio_embedding: {provider: hash, model: audio, dimension: 6}
chunking: {size: 8, overlap: 2}
""",
        encoding="utf-8",
    )
    return ModelGateway(path)


def settings() -> Settings:
    return Settings(
        PAIMON_TEXT_VECTOR_DIMENSION=4,
        PAIMON_IMAGE_VECTOR_DIMENSION=5,
        PAIMON_AUDIO_VECTOR_DIMENSION=6,
        MORPHLAKE_MAX_UPLOAD_MB=1,
    )


def test_upload_writes_descriptor_and_document_chunks(tmp_path: Path):
    objects, catalog = FakeObjects(), FakeCatalog()
    service = MorphLakeService(settings(), objects, catalog, models(tmp_path))
    result = service.upload(
        filename="report.txt",
        content_type="text/plain",
        body=b"abcdef ghijkl mnopqr",
        business_domain="risk",
        department="audit",
    )
    assert result["record_type"] == "file"
    assert result["object_bucket"] == "data"
    assert result["chunk_count"] >= 2
    assert all("body" not in record for record in catalog.records)
    assert all(len(record["text_embedding"]) == 4 for record in catalog.records[1:])


def test_upload_compensates_object_on_paimon_failure(tmp_path: Path):
    objects = FakeObjects()
    service = MorphLakeService(settings(), objects, FakeCatalog(fail=True), models(tmp_path))
    with pytest.raises(RuntimeError):
        service.upload(
            filename="report.txt",
            content_type="text/plain",
            body=b"content",
            business_domain="risk",
            department="audit",
        )
    assert len(objects.deleted) == 1


def test_upload_rejects_empty_body(tmp_path: Path):
    service = MorphLakeService(settings(), FakeObjects(), FakeCatalog(), models(tmp_path))
    with pytest.raises(MorphLakeError) as exc:
        service.upload(
            filename="report.txt",
            content_type="text/plain",
            body=b"",
            business_domain="risk",
            department="audit",
        )
    assert exc.value.code == "empty_file"
