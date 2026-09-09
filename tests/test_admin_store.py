from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    seeded = store.system_config()
    assert seeded["schema_version"] == "4"
    assert seeded["database_backend"] == "sqlite"
    assert seeded["default_rate_period_seconds"] == "60"
    assert seeded["default_admin_token_id"]
    assert store.ping()
    assert store.default_admin_token().startswith("mlk_")
    admin_row = next(
        row for row in store.list_tokens(reveal=True) if row["access_level"] == "admin"
    )
    assert admin_row["business_domain"] == "管理员"
    assert admin_row["department"] == "管理员"
    assert admin_row["plaintext"].startswith("mlk_")
    assert store.authenticate(admin_row["plaintext"]).access_level == "admin"
    with pytest.raises(MorphLakeError) as protected:
        store.set_token_status(admin_row["token_id"], "deleted")
    assert protected.value.code == "admin_key_protected"
    created = create_token(store)
    assert store.list_scope_options() == [{"business_domain": "risk", "department": "audit"}]
    assert {tuple(item.values()) for item in store.list_scope_options(include_admin=True)} == {
        ("risk", "audit"),
        ("管理员", "管理员"),
    }
    row = store.list_tokens()[0]
    assert created.plaintext not in str(row)
    assert store.authenticate(created.plaintext).department == "audit"
    revealed = next(
        item
        for item in store.list_tokens(reveal=True)
        if item["token_id"] == created.identity.token_id
    )
    assert revealed["plaintext"] == created.plaintext

    rotated = store.rotate_token(created.identity.token_id)
    assert rotated.plaintext != created.plaintext
    with pytest.raises(MorphLakeError) as invalid_old:
        store.authenticate(created.plaintext)
    assert invalid_old.value.code == "token_invalid"
    assert store.authenticate(rotated.plaintext).business_domain == "risk"

    store.set_token_status(created.identity.token_id, "disabled")
    with pytest.raises(MorphLakeError, match="disabled"):
        store.authenticate(rotated.plaintext)
    store.set_token_status(created.identity.token_id, "active")
    store.set_token_status(created.identity.token_id, "deleted")
    assert store.list_scope_options() == [{"business_domain": "risk", "department": "audit"}]
    with pytest.raises(MorphLakeError) as deleted:
        store.authenticate(rotated.plaintext)
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


def test_transfer_rates_aggregate_per_key_over_window(tmp_path: Path):
    store = make_store(tmp_path)
    identity = store.authenticate(create_token(store).plaintext)
    for operation, byte_count in (("upload", 2048), ("download", 1024), ("upload", 512)):
        store.record_transfer(
            identity=identity,
            operation=operation,
            filename="report.pdf",
            byte_count=byte_count,
            duration_ms=100,
            status="success",
            file_id="file-1",
            media_type="document",
            client_ip="10.0.0.8",
            user_agent="pytest",
        )
    rates = store.transfer_rates(24)
    assert len(rates) == 1
    assert rates[0]["request_count"] == 3
    assert rates[0]["byte_count"] == 3584
    assert rates[0]["bytes_per_second"] > 0
    assert rates[0]["bytes_per_second"] == round(3584 / (24 * 3600), 2)
    with pytest.raises(MorphLakeError, match="window_hours"):
        store.transfer_rates(0)


def test_transfer_rate_history_buckets_by_hour(tmp_path: Path):
    store = make_store(tmp_path)
    identity = store.authenticate(create_token(store).plaintext)
    store.record_transfer(
        identity=identity,
        operation="upload",
        filename="report.pdf",
        byte_count=2048,
        duration_ms=100,
        status="success",
        file_id="file-1",
        media_type="document",
        client_ip="10.0.0.8",
    )
    now = datetime.now(UTC)
    history = store.transfer_rate_history(
        start=(now - timedelta(hours=1)).isoformat(),
        end=(now + timedelta(minutes=1)).isoformat(),
        window_hours=1,
    )
    assert len(history) == 1
    assert history[0]["request_count"] == 1
    assert history[0]["byte_count"] == 2048
    assert history[0]["hour_bucket"] == now.isoformat()[:13]
    assert history[0]["bytes_per_second"] == round(2048 / 3600, 2)
