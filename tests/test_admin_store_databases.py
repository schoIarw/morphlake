from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from morphlake.admin_store import AdminStore
from morphlake.config import Settings
from morphlake.database import encrypt_secret


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_network_backend_creates_database_schema_and_seed_data(
    backend: str, monkeypatch, tmp_path: Path
):
    if os.getenv("MORPHLAKE_TEST_DATABASES") != "1":
        pytest.skip("network database integration test is disabled")

    password = "morphlake-test-password"
    auth: dict[str, str]
    if backend == "postgresql":
        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("TEST_DB_CREDENTIAL_KEY", key)
        auth = {
            "mode": "encrypt",
            "username": encrypt_secret("postgres", key),
            "password": encrypt_secret(password, key),
            "key_env": "TEST_DB_CREDENTIAL_KEY",
        }
    else:
        auth = {"mode": "native", "username": "root", "password": password}

    values = {
        "database": {
            "backend": backend,
            "host": "127.0.0.1",
            "port": 3306 if backend == "mysql" else 5432,
            "name": f"morphlake_ci_{backend}",
            "auth": auth,
            "auto_create_database": True,
            "maintenance_database": "postgres",
            "ssl_mode": "disable" if backend == "postgresql" else "",
            "pool_size": 2,
            "max_overflow": 1,
        }
    }
    config_path = tmp_path / f"{backend}.yaml"
    config_path.write_text(yaml.safe_dump(values), encoding="utf-8")
    settings = Settings(
        MORPHLAKE_ADMIN_DB_CONFIG=config_path,
        MORPHLAKE_TOKEN_PEPPER="integration-test-pepper",
    )
    store = AdminStore(settings)
    store.initialize()
    try:
        assert store.ping()
        assert store.system_config()["database_backend"] == backend
        created = store.create_token(
            business_domain="risk",
            department="audit",
            assignee_name="Database Test",
            phone="13800000000",
            notes="integration",
            allocated_by="pytest",
            period_seconds=60,
            upload_requests_limit=2,
            download_requests_limit=2,
            upload_bytes_limit=1_000,
            download_bytes_limit=1_000,
        )
        identity = store.authenticate(created.plaintext)
        assert identity.business_domain == "risk"
        store.consume_rate(identity, "upload", 10)
        event_id = store.record_transfer(
            identity=identity,
            operation="upload",
            filename="database-test.pdf",
            byte_count=10,
            duration_ms=1,
            status="success",
        )
        claim_id, events = store.claim_transfer_events(10)
        assert claim_id
        assert [event["event_id"] for event in events] == [event_id]
        store.mark_events_synced([event_id])
        assert store.unsynced_event_count() == 0
    finally:
        store.close()
