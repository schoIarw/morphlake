from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from morphlake.config import Settings
from morphlake.errors import ConfigurationError
from morphlake.partitioning import domain_shard
from morphlake.services.paimon_store import PaimonStore


def test_native_paimon_list_full_text_and_vector(tmp_path: Path):
    settings = Settings(
        PAIMON_WAREHOUSE=str(tmp_path / "warehouse"),
        PAIMON_DATABASE="morphlake_test",
        PAIMON_TABLE="assets",
        PAIMON_TEXT_TABLE="text_segments",
        PAIMON_IMAGE_TABLE="image_features",
        PAIMON_AUDIO_TABLE="audio_features",
        PAIMON_AUDIT_TABLE="transfer_audit",
        PAIMON_TEXT_VECTOR_DIMENSION=4,
        PAIMON_IMAGE_VECTOR_DIMENSION=4,
        PAIMON_AUDIO_VECTOR_DIMENSION=4,
        PAIMON_VECTOR_INDEX_TYPE="ivf-sq",
    )
    store = PaimonStore(settings)
    store.initialize()
    shard = domain_shard("risk", settings.paimon_domain_shards)
    base = {
        "file_id": "file-1",
        "business_domain": "risk",
        "department": "audit",
        "domain_shard": shard,
        "ingest_date": "2026-08-30",
        "created_at": "2026-08-30T00:00:00+00:00",
        "filename": "report.txt",
        "media_type": "document",
        "content_type": "text/plain",
        "file_size": 12,
        "object_bucket": "data",
        "object_key": "risk/report.txt",
        "object_etag": "etag",
        "chunk_count": 1,
        "content_sha256": "abc",
    }
    store.add(
        asset=base,
        text_segments=[
            {
                "segment_id": "file-1:0",
                "file_id": "file-1",
                "business_domain": "risk",
                "department": "audit",
                "domain_shard": shard,
                "ingest_date": "2026-08-30",
                "created_at": "2026-08-30T00:00:00+00:00",
                "filename": "report.txt",
                "media_type": "document",
                "record_type": "chunk",
                "chunk_index": 0,
                "content_text": "liquidity risk report",
                "segment_type": "document_chunk",
                "chunk_start": 0,
                "chunk_end": 12,
                "embedding_model": "test",
                "embedding_version": "1",
                "text_embedding": [1.0, 0.0, 0.0, 0.0],
            },
        ],
        image_features=[],
        audio_features=[],
    )
    for index, created_date, vector in (
        (2, "2026-08-31", [0.0, 1.0, 0.0, 0.0]),
        (3, "2026-09-01", [-1.0, 0.0, 0.0, 0.0]),
    ):
        another = {
            **base,
            "file_id": f"file-{index}",
            "filename": f"report-{index}.txt",
            "object_key": f"risk/report-{index}.txt",
            "created_at": f"{created_date}T00:00:00+00:00",
            "ingest_date": created_date,
        }
        store.add(
            asset=another,
            text_segments=[
                {
                    "segment_id": f"file-{index}:0",
                    "file_id": f"file-{index}",
                    "business_domain": "risk",
                    "department": "audit",
                    "domain_shard": shard,
                    "ingest_date": another["ingest_date"],
                    "created_at": another["created_at"],
                    "filename": another["filename"],
                    "media_type": "document",
                    "record_type": "chunk",
                    "chunk_index": 0,
                    "content_text": f"liquidity report {index}",
                    "segment_type": "document_chunk",
                    "chunk_start": 0,
                    "chunk_end": 12,
                    "embedding_model": "test",
                    "embedding_version": "1",
                    "text_embedding": vector,
                }
            ],
            image_features=[],
            audio_features=[],
        )
    store.maintain_indexes()
    listed = store.list_assets(
        media_type="document",
        business_domain="risk",
        department="audit",
        filename="report.txt",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=10,
        offset=0,
    )
    full_text = store.full_text_search(
        business_domain="risk",
        department="audit",
        keyword="liquidity",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=10,
    )
    vector = store.vector_search(
        business_domain="risk",
        department="audit",
        vector=[1.0, 0.0, 0.0, 0.0],
        vector_field="text",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=10,
    )
    assert len(listed) == 1
    assert len(full_text) == 2
    assert len(vector) == 2
    page = store.list_assets(
        media_type="document",
        business_domain="risk",
        department="audit",
        filename=None,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 1),
        limit=1,
        offset=1,
    )
    assert page[0]["file_id"] == "file-2"
    assert vector[0]["file_id"] == "file-1"
    fresh = {
        **base,
        "file_id": "file-4",
        "filename": "fresh.txt",
        "object_key": "risk/fresh.txt",
        "created_at": "2026-09-02T00:00:00+00:00",
        "ingest_date": "2026-09-02",
    }
    fresh_segment = {
        "segment_id": "file-4:0",
        "file_id": "file-4",
        "business_domain": "risk",
        "department": "audit",
        "domain_shard": shard,
        "ingest_date": "2026-09-02",
        "created_at": "2026-09-02T00:00:00+00:00",
        "filename": "fresh.txt",
        "media_type": "document",
        "record_type": "chunk",
        "chunk_index": 0,
        "content_text": "freshness sentinel",
        "segment_type": "document_chunk",
        "chunk_start": 0,
        "chunk_end": 18,
        "embedding_model": "test",
        "embedding_version": "1",
        "text_embedding": [0.5, 0.5, 0.0, 0.0],
    }
    store.add(
        asset=fresh,
        text_segments=[fresh_segment],
        image_features=[],
        audio_features=[],
    )
    store.maintain_indexes()
    refreshed = store.full_text_search(
        business_domain="risk",
        department="audit",
        keyword="freshness",
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 2),
        limit=10,
    )
    assert refreshed[0]["file_id"] == "file-4"
    store.add_transfer_events(
        [
            {
                "event_id": "event-1",
                "token_id": "token-1",
                "token_prefix": "abcd1234",
                "operation": "upload",
                "business_domain": "risk",
                "department": "audit",
                "domain_shard": shard,
                "ingest_date": "2026-08-30",
                "occurred_at": "2026-08-30T00:00:01+00:00",
                "file_id": "file-1",
                "filename": "report.txt",
                "media_type": "document",
                "byte_count": 12,
                "duration_ms": 20,
                "status": "success",
                "error_code": None,
                "client_ip": "127.0.0.1",
                "user_agent": "pytest",
            }
        ]
    )
    assert set(store.tables) == {"asset", "text", "image", "audio", "audit"}

    restarted = PaimonStore(settings)
    restarted.initialize()
    assert set(restarted.tables) == {"asset", "text", "image", "audio", "audit"}

    incompatible = settings.model_copy(update={"text_vector_dimension": 8})
    with pytest.raises(ConfigurationError, match="dimension must be 8"):
        PaimonStore(incompatible).initialize()
