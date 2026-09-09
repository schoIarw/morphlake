from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from morphlake.config import Settings
from morphlake.errors import ConfigurationError, NotFoundError, StorageError
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
        PAIMON_DELETION_TABLE="file_deletions",
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
                "segment_id": "file-1:summary",
                "file_id": "file-1",
                "business_domain": "risk",
                "department": "audit",
                "domain_shard": shard,
                "ingest_date": "2026-08-30",
                "created_at": "2026-08-30T00:00:00+00:00",
                "filename": "report.txt",
                "media_type": "document",
                "record_type": "file",
                "chunk_index": None,
                "content_text": "liquidity overview",
                "segment_type": "file_summary",
                "chunk_start": None,
                "chunk_end": None,
                "embedding_model": "test",
                "embedding_version": "1",
                "text_embedding": [1.0, 0.0, 0.0, 0.0],
            },
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
    filtered_page, filtered_total = store.list_assets_page(
        media_type=None,
        business_domain="risk",
        department="audit",
        filename="report",
        description="overview",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=1,
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
    assert filtered_total == 1
    assert filtered_page[0]["file_id"] == "file-1"
    assert len(full_text) == 2
    assert len(vector) == 2
    assert listed[0]["summary_text"] == "liquidity overview"
    assert listed[0]["embedding_preview"] == [1.0, 0.0, 0.0, 0.0]
    assert listed[0]["embedding_dimension"] == 4
    preview = store.get_preview("file-1", 100)
    assert preview["summary_text"] == "liquidity overview"
    assert preview["content_text"] == "liquidity risk report"
    summary_search = store.full_text_search(
        business_domain="risk",
        department="audit",
        keyword="overview",
        start_date=None,
        end_date=None,
        limit=10,
    )
    assert summary_search[0]["file_id"] == "file-1"
    assert summary_search[0]["summary_text"] == "liquidity overview"
    global_list = store.list_assets(
        media_type="document",
        business_domain=None,
        department=None,
        filename=None,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 1),
        limit=10,
        offset=0,
    )
    global_full_text = store.full_text_search(
        business_domain=None,
        department=None,
        keyword="liquidity",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 1),
        limit=10,
    )
    global_vector = store.vector_search(
        business_domain=None,
        department=None,
        vector=[1.0, 0.0, 0.0, 0.0],
        vector_field="text",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 1),
        limit=10,
    )
    assert len(global_list) == 3
    assert len(global_full_text) == 3
    assert len(global_vector) == 3
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
    assert vector[0]["match_rate"] == 1.0
    assert all("match_rate" in row for row in vector)
    rates = [row["match_rate"] for row in vector]
    assert rates == sorted(rates, reverse=True)
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
    assert [row["file_id"] for row in store.get_assets(["file-2", "file-1"])] == [
        "file-2",
        "file-1",
    ]
    store.delete_assets([base])
    with pytest.raises(Exception, match="does not exist"):
        store.get_asset("file-1")
    visible = store.list_assets(
        media_type="document",
        business_domain="risk",
        department="audit",
        filename=None,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 2),
        limit=10,
        offset=0,
    )
    assert {row["file_id"] for row in visible} == {"file-2", "file-3", "file-4"}
    assert all(
        row["file_id"] != "file-1"
        for row in store.full_text_search(
            business_domain="risk",
            department="audit",
            keyword="liquidity",
            start_date=None,
            end_date=None,
            limit=10,
        )
    )
    assert set(store.tables) == {"asset", "text", "image", "audio", "audit", "deletion"}

    restarted = PaimonStore(settings)
    restarted.initialize()
    assert set(restarted.tables) == {
        "asset",
        "text",
        "image",
        "audio",
        "audit",
        "deletion",
    }
    with pytest.raises(Exception, match="does not exist"):
        restarted.get_asset("file-1")

    incompatible = settings.model_copy(update={"text_vector_dimension": 8})
    with pytest.raises(ConfigurationError, match="dimension must be 8"):
        PaimonStore(incompatible).initialize()


def test_list_assets_page_paginates_by_offset_without_full_load(tmp_path: Path):
    settings = Settings(
        PAIMON_WAREHOUSE=str(tmp_path / "warehouse"),
        PAIMON_DATABASE="morphlake_test",
        PAIMON_TABLE="assets",
        PAIMON_TEXT_TABLE="text_segments",
        PAIMON_IMAGE_TABLE="image_features",
        PAIMON_AUDIO_TABLE="audio_features",
        PAIMON_AUDIT_TABLE="transfer_audit",
        PAIMON_DELETION_TABLE="file_deletions",
        PAIMON_TEXT_VECTOR_DIMENSION=4,
        PAIMON_IMAGE_VECTOR_DIMENSION=4,
        PAIMON_AUDIO_VECTOR_DIMENSION=4,
        PAIMON_VECTOR_INDEX_TYPE="ivf-sq",
    )
    store = PaimonStore(settings)
    store.initialize()
    shard = domain_shard("risk", settings.paimon_domain_shards)
    for index in range(5):
        created = f"2026-08-{30 - index:02d}T00:00:00+00:00"
        store.add(
            asset={
                "file_id": f"file-{index}",
                "business_domain": "risk",
                "department": "audit",
                "domain_shard": shard,
                "ingest_date": created[:10],
                "created_at": created,
                "filename": f"report-{index}.txt",
                "media_type": "document",
                "content_type": "text/plain",
                "file_size": 12,
                "object_bucket": "data",
                "object_key": f"risk/report-{index}.txt",
                "object_etag": "etag",
                "chunk_count": 0,
                "content_sha256": "abc",
            },
            text_segments=[],
            image_features=[],
            audio_features=[],
        )
    page_0, total_0 = store.list_assets_page(
        media_type=None,
        business_domain="risk",
        department="audit",
        filename=None,
        description=None,
        start_date=None,
        end_date=None,
        limit=2,
        offset=0,
    )
    page_1, total_1 = store.list_assets_page(
        media_type=None,
        business_domain="risk",
        department="audit",
        filename=None,
        description=None,
        start_date=None,
        end_date=None,
        limit=2,
        offset=2,
    )
    page_2, total_2 = store.list_assets_page(
        media_type=None,
        business_domain="risk",
        department="audit",
        filename=None,
        description=None,
        start_date=None,
        end_date=None,
        limit=2,
        offset=4,
    )
    beyond, total_beyond = store.list_assets_page(
        media_type=None,
        business_domain="risk",
        department="audit",
        filename=None,
        description=None,
        start_date=None,
        end_date=None,
        limit=2,
        offset=10,
    )
    assert total_0 == total_1 == total_2 == total_beyond == 5
    assert [row["file_id"] for row in page_0] == ["file-0", "file-1"]
    assert [row["file_id"] for row in page_1] == ["file-2", "file-3"]
    assert [row["file_id"] for row in page_2] == ["file-4"]
    assert beyond == []


def test_add_compensates_half_published_asset_with_tombstone(tmp_path: Path, monkeypatch):
    settings = Settings(
        PAIMON_WAREHOUSE=str(tmp_path / "warehouse"),
        PAIMON_DATABASE="morphlake_test",
        PAIMON_TABLE="assets",
        PAIMON_TEXT_TABLE="text_segments",
        PAIMON_IMAGE_TABLE="image_features",
        PAIMON_AUDIO_TABLE="audio_features",
        PAIMON_AUDIT_TABLE="transfer_audit",
        PAIMON_DELETION_TABLE="file_deletions",
        PAIMON_TEXT_VECTOR_DIMENSION=4,
        PAIMON_IMAGE_VECTOR_DIMENSION=4,
        PAIMON_AUDIO_VECTOR_DIMENSION=4,
        PAIMON_VECTOR_INDEX_TYPE="ivf-sq",
    )
    store = PaimonStore(settings)
    store.initialize()
    shard = domain_shard("risk", settings.paimon_domain_shards)
    asset = {
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
    real_require = store._require_table

    def failing_require(table_key):
        table = real_require(table_key)
        if table_key == "text":
            table = SimpleNamespace(
                raw_table=table.raw_table,
                add=lambda arrow: (_ for _ in ()).throw(RuntimeError("commit failed")),
            )
        return table

    monkeypatch.setattr(store, "_require_table", failing_require)
    with pytest.raises(StorageError, match="Paimon write failed"):
        store.add(
            asset=asset,
            text_segments=[
                {
                    "segment_id": "file-1:summary",
                    "file_id": "file-1",
                    "business_domain": "risk",
                    "department": "audit",
                    "domain_shard": shard,
                    "ingest_date": "2026-08-30",
                    "created_at": "2026-08-30T00:00:00+00:00",
                    "filename": "report.txt",
                    "media_type": "document",
                    "record_type": "file",
                    "chunk_index": None,
                    "content_text": "liquidity overview",
                    "segment_type": "file_summary",
                    "chunk_start": None,
                    "chunk_end": None,
                    "embedding_model": "test",
                    "embedding_version": "1",
                    "text_embedding": [1.0, 0.0, 0.0, 0.0],
                }
            ],
            image_features=[],
            audio_features=[],
        )
    with pytest.raises(NotFoundError, match="does not exist"):
        store.get_asset("file-1")
    page, total = store.list_assets_page(
        media_type=None,
        business_domain="risk",
        department="audit",
        filename=None,
        description=None,
        start_date=None,
        end_date=None,
        limit=10,
        offset=0,
    )
    assert total == 0
    assert page == []


def test_vector_match_rate_ranks_descending(tmp_path: Path):
    settings = Settings(
        PAIMON_WAREHOUSE=str(tmp_path / "warehouse"),
        PAIMON_DATABASE="morphlake_test",
        PAIMON_TABLE="assets",
        PAIMON_TEXT_TABLE="text_segments",
        PAIMON_IMAGE_TABLE="image_features",
        PAIMON_AUDIO_TABLE="audio_features",
        PAIMON_AUDIT_TABLE="transfer_audit",
        PAIMON_DELETION_TABLE="file_deletions",
        PAIMON_TEXT_VECTOR_DIMENSION=4,
        PAIMON_IMAGE_VECTOR_DIMENSION=4,
        PAIMON_AUDIO_VECTOR_DIMENSION=4,
        PAIMON_VECTOR_INDEX_TYPE="ivf-sq",
    )
    store = PaimonStore(settings)
    store.initialize()
    shard = domain_shard("risk", settings.paimon_domain_shards)
    for index, vector in enumerate(
        ([1.0, 0.0, 0.0, 0.0], [0.8, 0.6, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [-0.5, 0.0, 0.0, 0.0])
    ):
        created = f"2026-08-{30 - index:02d}T00:00:00+00:00"
        store.add(
            asset={
                "file_id": f"file-{index}",
                "business_domain": "risk",
                "department": "audit",
                "domain_shard": shard,
                "ingest_date": created[:10],
                "created_at": created,
                "filename": f"report-{index}.txt",
                "media_type": "document",
                "content_type": "text/plain",
                "file_size": 12,
                "object_bucket": "data",
                "object_key": f"risk/report-{index}.txt",
                "object_etag": "etag",
                "chunk_count": 1,
                "content_sha256": "abc",
            },
            text_segments=[
                {
                    "segment_id": f"file-{index}:0",
                    "file_id": f"file-{index}",
                    "business_domain": "risk",
                    "department": "audit",
                    "domain_shard": shard,
                    "ingest_date": created[:10],
                    "created_at": created,
                    "filename": f"report-{index}.txt",
                    "media_type": "document",
                    "record_type": "chunk",
                    "chunk_index": 0,
                    "content_text": f"queryable content {index}",
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
    hits = store.vector_search(
        business_domain="risk",
        department="audit",
        vector=[1.0, 0.0, 0.0, 0.0],
        vector_field="text",
        start_date=None,
        end_date=None,
        limit=10,
    )
    assert [hit["match_rate"] for hit in hits] == [1.0, 0.8, 0.0, 0.0]
    assert [hit["file_id"] for hit in hits] == ["file-0", "file-1", "file-2", "file-3"]
    assert hits[3]["match_rate"] == 0.0  # negative cosine is clamped to 0%
