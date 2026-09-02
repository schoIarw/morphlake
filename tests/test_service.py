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
        self.asset = None
        self.text_segments = []
        self.image_features = []
        self.audio_features = []
        self.vector_query = None

    def initialize(self):
        return None

    def add(self, *, asset, text_segments, image_features, audio_features):
        if self.fail:
            raise RuntimeError("commit failed")
        self.asset = asset
        self.text_segments.extend(text_segments)
        self.image_features.extend(image_features)
        self.audio_features.extend(audio_features)

    def maintain_indexes(self):
        return None

    def ping(self):
        return None

    def vector_search(self, **values):
        self.vector_query = values
        return []


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
    assert result["object_bucket"] == "data"
    assert result["chunk_count"] >= 2
    assert catalog.asset == result
    assert "body" not in catalog.asset
    assert all("object_key" not in row for row in catalog.text_segments)
    assert all(len(row["text_embedding"]) == 4 for row in catalog.text_segments)
    assert all(row["domain_shard"] == result["domain_shard"] for row in catalog.text_segments)


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


def test_typed_upload_rejects_mismatch_before_object_write(tmp_path: Path):
    objects = FakeObjects()
    service = MorphLakeService(settings(), objects, FakeCatalog(), models(tmp_path))
    with pytest.raises(MorphLakeError) as exc:
        service.upload(
            filename="photo.png",
            content_type="image/png",
            body=b"image",
            business_domain="risk",
            department="audit",
            expected_media_type="document",
        )
    assert exc.value.code == "media_type_mismatch"
    assert objects.values == {}


@pytest.mark.parametrize(
    ("filename", "content_type", "body", "expected_field", "dimension"),
    [
        ("query.txt", "text/plain", b"document query", "text", 4),
        ("query.png", "image/png", b"image query", "image", 5),
        ("query.mp3", "audio/mpeg", b"audio query", "audio", 6),
    ],
)
def test_query_file_is_vectorized_by_modality_and_uses_top_10(
    tmp_path: Path,
    filename: str,
    content_type: str,
    body: bytes,
    expected_field: str,
    dimension: int,
):
    catalog = FakeCatalog()
    objects = FakeObjects()
    service = MorphLakeService(settings(), objects, catalog, models(tmp_path))

    assert (
        service.vector_search_file(
            filename=filename,
            content_type=content_type,
            body=body,
            business_domain="risk",
        )
        == []
    )

    assert catalog.vector_query["vector_field"] == expected_field
    assert len(catalog.vector_query["vector"]) == dimension
    assert catalog.vector_query["business_domain"] == "risk"
    assert catalog.vector_query["limit"] == 10
    assert objects.values == {}
