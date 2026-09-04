"""Management database configuration, credential protection, and engine creation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool


class DatabaseAuth(BaseModel):
    """Database login material as plaintext or authenticated ciphertext."""

    mode: Literal["native", "encrypt"] = "native"
    username: str = ""
    password: str = ""
    key_env: str = "MORPHLAKE_DB_CREDENTIAL_KEY"


class DatabaseConfig(BaseModel):
    """Validated management database configuration."""

    backend: Literal["sqlite", "mysql", "postgresql"] = "sqlite"
    path: Path = Path("data/morphlake-admin.db")
    host: str = "127.0.0.1"
    port: int | None = None
    name: str = "morphlake"
    auth: DatabaseAuth = Field(default_factory=DatabaseAuth)
    auto_create_database: bool = True
    maintenance_database: str = "postgres"
    connect_timeout_seconds: int = Field(10, ge=1, le=300)
    pool_size: int = Field(10, ge=1, le=500)
    max_overflow: int = Field(20, ge=0, le=1_000)
    pool_recycle_seconds: int = Field(1_800, ge=60)
    ssl_mode: str = "prefer"

    @model_validator(mode="after")
    def validate_network_database(self) -> DatabaseConfig:
        if self.backend != "sqlite":
            if self.port is None:
                self.port = 3306 if self.backend == "mysql" else 5432
            if not self.host.strip() or not self.name.strip():
                raise ValueError("Network database host and name are required")
            if not self.auth.username or not self.auth.password:
                raise ValueError("Network database username and password are required")
        return self

    def credentials(self) -> tuple[str, str]:
        if self.backend == "sqlite":
            return "", ""
        if self.auth.mode == "native":
            return self.auth.username, self.auth.password
        key = os.getenv(self.auth.key_env)
        if not key:
            raise ValueError(
                f"Encrypted database credentials require environment variable {self.auth.key_env}"
            )
        return decrypt_secret(self.auth.username, key), decrypt_secret(self.auth.password, key)

    def url(self, *, database: str | None = None) -> URL:
        if self.backend == "sqlite":
            return URL.create("sqlite+pysqlite", database=str(self.path))
        username, password = self.credentials()
        target_database = self.name if database is None else database
        if self.backend == "mysql":
            return URL.create(
                "mysql+pymysql",
                username=username,
                password=password,
                host=self.host,
                port=self.port,
                database=target_database or None,
                query={"charset": "utf8mb4"},
            )
        query = {"sslmode": self.ssl_mode} if self.ssl_mode else {}
        return URL.create(
            "postgresql+psycopg",
            username=username,
            password=password,
            host=self.host,
            port=self.port,
            database=target_database,
            query=query,
        )


def load_database_config(path: Path, sqlite_path_override: Path | None = None) -> DatabaseConfig:
    """Load a database YAML profile, with a legacy SQLite path override for compatibility."""
    if sqlite_path_override is not None:
        return DatabaseConfig(backend="sqlite", path=sqlite_path_override)
    if not path.exists():
        raise FileNotFoundError(f"Database configuration does not exist: {path}")
    with path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    values = raw.get("database", raw)
    if not isinstance(values, dict):
        raise ValueError("Database configuration must contain a 'database' mapping")
    return DatabaseConfig.model_validate(values)


def encrypt_secret(plaintext: str, key: str | bytes) -> str:
    """Encrypt one small secret and wrap it for safe YAML storage."""
    token = Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"ENC[{token}]"


def decrypt_secret(ciphertext: str, key: str | bytes) -> str:
    """Decrypt and authenticate an ENC[...] value without exposing it in errors."""
    if not ciphertext.startswith("ENC[") or not ciphertext.endswith("]"):
        raise ValueError("Encrypted database credential must use ENC[...] format")
    try:
        value = Fernet(key).decrypt(ciphertext[4:-1].encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Database credential cannot be decrypted with the configured key") from exc
    return value.decode("utf-8")


def create_management_engine(config: DatabaseConfig) -> Engine:
    """Create the configured database when permitted, then return a pooled target engine."""
    if config.backend == "sqlite":
        config.path.parent.mkdir(parents=True, exist_ok=True)
    elif config.auto_create_database:
        _create_network_database(config)

    if config.backend == "sqlite":
        engine = create_engine(
            config.url(),
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine

    connect_args = {"connect_timeout": config.connect_timeout_seconds}
    return create_engine(
        config.url(),
        pool_pre_ping=True,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_recycle=config.pool_recycle_seconds,
        connect_args=connect_args,
    )


def _create_network_database(config: DatabaseConfig) -> None:
    bootstrap_database = "" if config.backend == "mysql" else config.maintenance_database
    engine = create_engine(
        config.url(database=bootstrap_database),
        poolclass=NullPool,
        connect_args={"connect_timeout": config.connect_timeout_seconds},
    )
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            quoted_name = connection.dialect.identifier_preparer.quote(config.name)
            if config.backend == "mysql":
                connection.exec_driver_sql(
                    f"CREATE DATABASE IF NOT EXISTS {quoted_name} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                return
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": config.name}
            ).scalar_one_or_none()
            if exists is None:
                try:
                    connection.exec_driver_sql(f"CREATE DATABASE {quoted_name} ENCODING 'UTF8'")
                except DBAPIError:
                    # Both independently started containers may race on first boot.
                    exists = connection.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": config.name},
                    ).scalar_one_or_none()
                    if exists is None:
                        raise
    finally:
        engine.dispose()
