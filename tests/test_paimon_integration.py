from __future__ import annotations

from datetime import date
from pathlib import Path

from morphlake.config import Settings
from morphlake.services.paimon_store import PaimonStore


def test_native_paimon_list_full_text_and_vector(tmp_path: Path):
    settings = Settings(
        PAIMON_WAREHOUSE=str(tmp_path / "warehouse"),
        PAIMON_DATABASE="morphlake_test",
        PAIMON_TABLE="assets",
        PAIMON_TEXT_VECTOR_DIMENSION=4,
        PAIMON_IMAGE_VECTOR_DIMENSION=4,
        PAIMON_AUDIO_VECTOR_DIMENSION=4,
    )
    store = PaimonStore(settings)
    store.initialize()
    base = {
        "file_id": "file-1",
        "business_domain": "risk",
        "department": "audit",
        "upload_month": "2026-08",
        "created_at": "2026-08-30T00:00:00+00:00",
        "filename": "report.txt",
        "media_type": "document",
        "content_type": "text/plain",
        "file_size": 12,
        "object_bucket": "data",
        "object_key": "risk/report.txt",
        "object_etag": "etag",
        "chunk_count": 1,
    }
    store.add(
        [
            {
                **base,
                "row_id": "file-1",
                "record_type": "file",
                "chunk_index": None,
                "chunk_start": None,
                "chunk_end": None,
                "content_text": None,
                "text_embedding": None,
                "image_embedding": None,
                "audio_embedding": None,
            },
            {
                **base,
                "row_id": "file-1:0",
                "record_type": "chunk",
                "chunk_index": 0,
                "chunk_start": 0,
                "chunk_end": 12,
                "content_text": "liquidity risk report",
                "text_embedding": [1.0, 0.0, 0.0, 0.0],
                "image_embedding": None,
                "audio_embedding": None,
            },
        ]
    )
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
