from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from morphlake.database import (
    DatabaseConfig,
    decrypt_secret,
    encrypt_secret,
    load_database_config,
)


def test_native_mysql_url_preserves_special_credentials():
    config = DatabaseConfig.model_validate(
        {
            "backend": "mysql",
            "host": "mysql.internal",
            "name": "morphlake",
            "auth": {
                "mode": "native",
                "username": "user@domain",
                "password": "p@ss:/word",
            },
        }
    )
    url = config.url()
    assert url.drivername == "mysql+pymysql"
    assert url.username == "user@domain"
    assert url.password == "p@ss:/word"
    assert url.port == 3306


def test_encrypted_postgresql_credentials(monkeypatch, tmp_path: Path):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("TEST_DB_KEY", key)
    path = tmp_path / "database.yaml"
    path.write_text(
        "database:\n"
        "  backend: postgresql\n"
        "  host: postgres.internal\n"
        "  name: morphlake\n"
        "  ssl_mode: require\n"
        "  auth:\n"
        "    mode: encrypt\n"
        f"    username: {encrypt_secret('db-user', key)}\n"
        f"    password: {encrypt_secret('db-password', key)}\n"
        "    key_env: TEST_DB_KEY\n",
        encoding="utf-8",
    )
    config = load_database_config(path)
    assert config.credentials() == ("db-user", "db-password")
    assert config.url().drivername == "postgresql+psycopg"
    assert config.url().port == 5432
    assert config.url().query["sslmode"] == "require"


def test_encrypted_credentials_reject_missing_or_wrong_key(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    cipher = encrypt_secret("secret", key)
    assert decrypt_secret(cipher, key) == "secret"
    with pytest.raises(ValueError, match="cannot be decrypted"):
        decrypt_secret(cipher, Fernet.generate_key())

    config = DatabaseConfig.model_validate(
        {
            "backend": "mysql",
            "auth": {
                "mode": "encrypt",
                "username": cipher,
                "password": cipher,
                "key_env": "MISSING_TEST_KEY",
            },
        }
    )
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)
    with pytest.raises(ValueError, match="MISSING_TEST_KEY"):
        config.credentials()


def test_legacy_sqlite_path_override(tmp_path: Path):
    path = tmp_path / "legacy.db"
    config = load_database_config(tmp_path / "missing.yaml", path)
    assert config.backend == "sqlite"
    assert config.path == path
