from __future__ import annotations

from pathlib import Path

import pytest

from morphlake.admin_store import AdminStore
from morphlake.config import Settings
from morphlake.errors import MorphLakeError


def make_store(tmp_path: Path) -> AdminStore:
    store = AdminStore(
        Settings(
            MORPHLAKE_ADMIN_DB_PATH=tmp_path / "admin.db",
            MORPHLAKE_TOKEN_PEPPER="test-pepper",
        )
    )
    store.initialize()
    return store


def create_token(store: AdminStore):
    return store.create_token(
        business_domain="risk",
        department="audit",
        assignee_name="Alice",
        phone="13800000000",
        notes="batch application",
        allocated_by="admin",
        period_seconds=60,
        upload_requests_limit=2,
        download_requests_limit=3,
        upload_bytes_limit=10,
        download_bytes_limit=20,
    )


def test_token_is_hashed_and_lifecycle_is_enforced(tmp_path: Path):
    store = make_store(tmp_path)
    created = create_token(store)
    row = store.list_tokens()[0]
    assert created.plaintext not in str(row)
    assert store.authenticate(created.plaintext).department == "audit"

    store.set_token_status(created.identity.token_id, "disabled")
    with pytest.raises(MorphLakeError, match="disabled"):
        store.authenticate(created.plaintext)
    store.set_token_status(created.identity.token_id, "active")
    store.set_token_status(created.identity.token_id, "deleted")
    with pytest.raises(MorphLakeError) as deleted:
        store.authenticate(created.plaintext)
    assert deleted.value.code == "token_deleted"
    with pytest.raises(MorphLakeError, match="cannot be changed"):
        store.set_token_status(created.identity.token_id, "active")


def test_atomic_rate_limits_and_updates(tmp_path: Path):
    store = make_store(tmp_path)
    created = create_token(store)
    identity = store.authenticate(created.plaintext)
    store.consume_rate(identity, "upload", 4)
    store.consume_rate(identity, "upload", 6)
    with pytest.raises(MorphLakeError) as limited:
        store.consume_rate(identity, "upload", 1)
    assert limited.value.status_code == 429
    assert "Retry-After" in limited.value.headers

    store.update_token_limits(
        identity.token_id,
        period_seconds=120,
        upload_requests_limit=9,
        download_requests_limit=8,
        upload_bytes_limit=700,
        download_bytes_limit=600,
    )
    updated = store.authenticate(created.plaintext)
    assert updated.period_seconds == 120
    assert updated.upload_requests_limit == 9


def test_transfer_outbox_and_period_stats(tmp_path: Path):
    store = make_store(tmp_path)
    identity = store.authenticate(create_token(store).plaintext)
    event_id = store.record_transfer(
        identity=identity,
        operation="upload",
        filename="report.pdf",
        byte_count=123,
        duration_ms=45,
        status="success",
        file_id="file-1",
        media_type="document",
    )
    pending = store.pending_transfer_events(10)
    assert pending[0]["event_id"] == event_id
    stats = store.transfer_stats("day")
    assert stats[0]["request_count"] == 1
    assert stats[0]["byte_count"] == 123
    store.mark_events_synced([event_id])
    assert store.unsynced_event_count() == 0
