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
    store.maintain_indexes()
    listed = store.list_assets(
        media_type="document",
        business_domain="risk",
        department="audit",
        filename="report",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=10,
        offset=0,
    )
    full_text = store.full_text_search(
        business_domain="risk",
        keyword="liquidity",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=10,
    )
    vector = store.vector_search(
        business_domain="risk",
        vector=[1.0, 0.0, 0.0, 0.0],
        vector_field="text",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        limit=10,
    )
    assert len(listed) == 1
    assert len(full_text) == 1
    assert len(vector) == 1
    assert set(store.tables) == {"asset", "text", "image", "audio"}

    restarted = PaimonStore(settings)
    restarted.initialize()
    assert set(restarted.tables) == {"asset", "text", "image", "audio"}

    incompatible = settings.model_copy(update={"text_vector_dimension": 8})
    with pytest.raises(ConfigurationError, match="dimension must be 8"):
        PaimonStore(incompatible).initialize()
