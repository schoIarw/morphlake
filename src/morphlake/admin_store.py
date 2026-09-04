"""Portable token administration, rate limiting, and transfer outbox storage."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from morphlake.config import Settings
from morphlake.database import DatabaseConfig, create_management_engine, load_database_config
from morphlake.errors import MorphLakeError
from morphlake.partitioning import domain_shard


@dataclass(frozen=True)
class TokenIdentity:
    token_id: str
    token_prefix: str
    business_domain: str
    department: str
    assignee_name: str
    phone: str
    status: str
    period_seconds: int
    upload_requests_limit: int
    download_requests_limit: int
    upload_bytes_limit: int
    download_bytes_limit: int


@dataclass(frozen=True)
class CreatedToken:
    identity: TokenIdentity
    plaintext: str


@dataclass(frozen=True)
class RateDecision:
    remaining_requests: int | None
    remaining_bytes: int | None
    reset_at_epoch: int


METADATA = MetaData()

API_TOKENS = Table(
    "api_tokens",
    METADATA,
    Column("token_id", String(36), primary_key=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("token_prefix", String(16), nullable=False),
    Column("business_domain", String(255), nullable=False),
    Column("department", String(255), nullable=False),
    Column("assignee_name", String(255), nullable=False),
    Column("phone", String(64), nullable=False),
    Column("notes", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("allocated_by", String(255), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("expires_at", String(40)),
    Column("last_used_at", String(40)),
    Column("period_seconds", Integer, nullable=False),
    Column("upload_requests_limit", BigInteger, nullable=False),
    Column("download_requests_limit", BigInteger, nullable=False),
    Column("upload_bytes_limit", BigInteger, nullable=False),
    Column("download_bytes_limit", BigInteger, nullable=False),
    CheckConstraint("status IN ('active','disabled','deleted')", name="ck_api_tokens_status"),
)
Index(
    "idx_api_tokens_scope",
    API_TOKENS.c.business_domain,
    API_TOKENS.c.department,
    API_TOKENS.c.status,
)

RATE_COUNTERS = Table(
    "rate_counters",
    METADATA,
    Column("token_id", String(36), primary_key=True),
    Column("operation", String(16), primary_key=True),
    Column("window_start", BigInteger, primary_key=True),
    Column("request_count", BigInteger, nullable=False),
    Column("byte_count", BigInteger, nullable=False),
    CheckConstraint("operation IN ('upload','download')", name="ck_rate_counters_operation"),
)

TRANSFER_EVENTS = Table(
    "transfer_events",
    METADATA,
    Column("event_id", String(36), primary_key=True),
    Column("token_id", String(36), nullable=False),
    Column("token_prefix", String(16), nullable=False),
    Column("operation", String(16), nullable=False),
    Column("business_domain", String(255), nullable=False),
    Column("department", String(255), nullable=False),
    Column("domain_shard", Integer, nullable=False),
    Column("ingest_date", String(10), nullable=False),
    Column("occurred_at", String(40), nullable=False),
    Column("file_id", String(64)),
    Column("filename", Text, nullable=False),
    Column("media_type", String(32)),
    Column("byte_count", BigInteger, nullable=False),
    Column("duration_ms", BigInteger, nullable=False),
    Column("status", String(32), nullable=False),
    Column("error_code", String(128)),
    Column("client_ip", String(64)),
    Column("user_agent", String(512)),
    Column("claim_id", String(36)),
    Column("claimed_at", String(40)),
    Column("synced_at", String(40)),
    CheckConstraint("operation IN ('upload','download')", name="ck_transfer_events_operation"),
)
Index("idx_transfer_events_unsynced", TRANSFER_EVENTS.c.synced_at, TRANSFER_EVENTS.c.occurred_at)
Index(
    "idx_transfer_events_scope_time",
    TRANSFER_EVENTS.c.business_domain,
    TRANSFER_EVENTS.c.department,
    TRANSFER_EVENTS.c.occurred_at,
)

TRANSFER_DAILY_STATS = Table(
    "transfer_daily_stats",
    METADATA,
    Column("stat_date", String(10), primary_key=True),
    Column("token_id", String(36), primary_key=True),
    Column("token_prefix", String(16), nullable=False),
    Column("business_domain", String(255), nullable=False),
    Column("department", String(255), nullable=False),
    Column("operation", String(16), primary_key=True),
    Column("status", String(32), primary_key=True),
    Column("request_count", BigInteger, nullable=False),
    Column("byte_count", BigInteger, nullable=False),
)

SYSTEM_CONFIG = Table(
    "system_config",
    METADATA,
    Column("config_key", String(128), primary_key=True),
    Column("config_value", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)


class AdminStore:
    """Transactional management store supporting SQLite, MySQL, and PostgreSQL."""

    _SCHEMA_LOCK_ID = 6_735_984_797_511_706_337
    _SCHEMA_LOCK_NAME = "morphlake_management_schema"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.database: DatabaseConfig = load_database_config(
            settings.admin_db_config, settings.admin_db_path
        )
        self.backend = self.database.backend
        self._engine: Engine | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """Create the database, tables, migrations, and required seed values idempotently."""
        with self._lock:
            if self._engine is None:
                self._engine = create_management_engine(self.database)
            self._initialize_schema()

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def ping(self) -> bool:
        with self._engine_required().connect() as connection:
            return connection.execute(select(1)).scalar_one() == 1

    def system_config(self) -> dict[str, str]:
        with self._engine_required().connect() as connection:
            rows = connection.execute(select(SYSTEM_CONFIG)).mappings().all()
        return {row["config_key"]: row["config_value"] for row in rows}

    def create_token(
        self,
        *,
        business_domain: str,
        department: str,
        assignee_name: str,
        phone: str,
        notes: str,
        allocated_by: str,
        period_seconds: int,
        upload_requests_limit: int,
        download_requests_limit: int,
        upload_bytes_limit: int,
        download_bytes_limit: int,
        expires_at: str | None = None,
    ) -> CreatedToken:
        values = [business_domain, department, assignee_name, phone]
        if any(not value.strip() for value in values):
            raise MorphLakeError(
                "invalid_token_assignment",
                "business_domain, department, assignee_name, and phone are required",
            )
        limits = [
            period_seconds,
            upload_requests_limit,
            download_requests_limit,
            upload_bytes_limit,
            download_bytes_limit,
        ]
        if period_seconds <= 0 or any(value < 0 for value in limits[1:]):
            raise MorphLakeError("invalid_rate_limit", "Rate limits must be non-negative")

        token_id = str(uuid.uuid4())
        token_prefix = secrets.token_hex(4)
        plaintext = f"mlk_{token_prefix}_{secrets.token_urlsafe(32)}"
        now = datetime.now(UTC).isoformat()
        identity = TokenIdentity(
            token_id=token_id,
            token_prefix=token_prefix,
            business_domain=business_domain.strip(),
            department=department.strip(),
            assignee_name=assignee_name.strip(),
            phone=phone.strip(),
            status="active",
            period_seconds=period_seconds,
            upload_requests_limit=upload_requests_limit,
            download_requests_limit=download_requests_limit,
            upload_bytes_limit=upload_bytes_limit,
            download_bytes_limit=download_bytes_limit,
        )
        with self._transaction() as connection:
            connection.execute(
                insert(API_TOKENS).values(
                    token_id=token_id,
                    token_hash=self._token_hash(plaintext),
                    token_prefix=token_prefix,
                    business_domain=identity.business_domain,
                    department=identity.department,
                    assignee_name=identity.assignee_name,
                    phone=identity.phone,
                    notes=notes.strip(),
                    status="active",
                    allocated_by=allocated_by,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at or None,
                    last_used_at=None,
                    period_seconds=period_seconds,
                    upload_requests_limit=upload_requests_limit,
                    download_requests_limit=download_requests_limit,
                    upload_bytes_limit=upload_bytes_limit,
                    download_bytes_limit=download_bytes_limit,
                )
            )
        return CreatedToken(identity=identity, plaintext=plaintext)

    def authenticate(self, plaintext: str | None) -> TokenIdentity:
        if not plaintext:
            raise MorphLakeError("token_required", "API token is required", 401)
        with self._transaction() as connection:
            row = (
                connection.execute(
                    select(API_TOKENS).where(API_TOKENS.c.token_hash == self._token_hash(plaintext))
                )
                .mappings()
                .first()
            )
            if row is None:
                raise MorphLakeError("token_invalid", "API token is invalid", 401)
            if row["status"] == "disabled":
                raise MorphLakeError("token_disabled", "API token is disabled", 403)
            if row["status"] == "deleted":
                raise MorphLakeError("token_deleted", "API token has been deleted", 403)
            if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
                raise MorphLakeError("token_expired", "API token has expired", 403)
            connection.execute(
                update(API_TOKENS)
                .where(API_TOKENS.c.token_id == row["token_id"])
                .values(last_used_at=datetime.now(UTC).isoformat())
            )
        return self._identity(row)

    def list_tokens(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        statement = select(API_TOKENS).order_by(API_TOKENS.c.created_at.desc())
        if not include_deleted:
            statement = statement.where(API_TOKENS.c.status != "deleted")
        with self._engine_required().connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [dict(row) for row in rows]

    def set_token_status(self, token_id: str, status: str) -> None:
        if status not in {"active", "disabled", "deleted"}:
            raise MorphLakeError("invalid_token_status", "Unsupported token status")
        criteria = API_TOKENS.c.token_id == token_id
        if status != "deleted":
            criteria = and_(criteria, API_TOKENS.c.status != "deleted")
        with self._transaction() as connection:
            result = connection.execute(
                update(API_TOKENS)
                .where(criteria)
                .values(status=status, updated_at=datetime.now(UTC).isoformat())
            )
            if result.rowcount != 1:
                existing = connection.execute(
                    select(API_TOKENS.c.status).where(API_TOKENS.c.token_id == token_id)
                ).scalar_one_or_none()
                if existing == "deleted":
                    raise MorphLakeError("token_deleted", "Deleted tokens cannot be changed", 409)
                raise MorphLakeError("token_not_found", "Token does not exist", 404)

    def update_token_limits(
        self,
        token_id: str,
        *,
        period_seconds: int,
        upload_requests_limit: int,
        download_requests_limit: int,
        upload_bytes_limit: int,
        download_bytes_limit: int,
    ) -> None:
        limits = [
            upload_requests_limit,
            download_requests_limit,
            upload_bytes_limit,
            download_bytes_limit,
        ]
        if period_seconds <= 0 or any(value < 0 for value in limits):
            raise MorphLakeError("invalid_rate_limit", "Rate limits must be non-negative")
        with self._transaction() as connection:
            result = connection.execute(
                update(API_TOKENS)
                .where(and_(API_TOKENS.c.token_id == token_id, API_TOKENS.c.status != "deleted"))
                .values(
                    period_seconds=period_seconds,
                    upload_requests_limit=upload_requests_limit,
                    download_requests_limit=download_requests_limit,
                    upload_bytes_limit=upload_bytes_limit,
                    download_bytes_limit=download_bytes_limit,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            if result.rowcount != 1:
                raise MorphLakeError("token_not_found", "Active token does not exist", 404)

    def consume_rate(
        self, identity: TokenIdentity, operation: str, byte_count: int
    ) -> RateDecision:
        request_limit, byte_limit = self._limits(identity, operation)
        now_epoch = int(time.time())
        window_start = now_epoch - now_epoch % identity.period_seconds
        reset_at = window_start + identity.period_seconds
        key = {
            "token_id": identity.token_id,
            "operation": operation,
            "window_start": window_start,
        }
        with self._lock, self._transaction(immediate=True) as connection:
            connection.execute(
                delete(RATE_COUNTERS).where(
                    and_(
                        RATE_COUNTERS.c.token_id == identity.token_id,
                        RATE_COUNTERS.c.window_start
                        < now_epoch - max(86_400, identity.period_seconds * 2),
                    )
                )
            )
            connection.execute(
                self._insert_do_nothing(
                    RATE_COUNTERS,
                    {**key, "request_count": 0, "byte_count": 0},
                    ["token_id", "operation", "window_start"],
                )
            )
            statement = select(RATE_COUNTERS.c.request_count, RATE_COUNTERS.c.byte_count).where(
                and_(
                    RATE_COUNTERS.c.token_id == identity.token_id,
                    RATE_COUNTERS.c.operation == operation,
                    RATE_COUNTERS.c.window_start == window_start,
                )
            )
            if self.backend != "sqlite":
                statement = statement.with_for_update()
            row = connection.execute(statement).mappings().one()
            requests = int(row["request_count"])
            used_bytes = int(row["byte_count"])
            if request_limit and requests + 1 > request_limit:
                raise self._rate_error(operation, reset_at)
            if byte_limit and used_bytes + byte_count > byte_limit:
                raise self._rate_error(operation, reset_at)
            connection.execute(
                update(RATE_COUNTERS)
                .where(
                    and_(
                        RATE_COUNTERS.c.token_id == identity.token_id,
                        RATE_COUNTERS.c.operation == operation,
                        RATE_COUNTERS.c.window_start == window_start,
                    )
                )
                .values(
                    request_count=RATE_COUNTERS.c.request_count + 1,
                    byte_count=RATE_COUNTERS.c.byte_count + byte_count,
                )
            )
        return RateDecision(
            remaining_requests=None if not request_limit else request_limit - requests - 1,
            remaining_bytes=None if not byte_limit else byte_limit - used_bytes - byte_count,
            reset_at_epoch=reset_at,
        )

    def refund_rate(self, identity: TokenIdentity, operation: str, byte_count: int) -> None:
        now_epoch = int(time.time())
        window_start = now_epoch - now_epoch % identity.period_seconds
        with self._transaction() as connection:
            connection.execute(
                update(RATE_COUNTERS)
                .where(
                    and_(
                        RATE_COUNTERS.c.token_id == identity.token_id,
                        RATE_COUNTERS.c.operation == operation,
                        RATE_COUNTERS.c.window_start == window_start,
                    )
                )
                .values(
                    request_count=case(
                        (RATE_COUNTERS.c.request_count <= 1, 0),
                        else_=RATE_COUNTERS.c.request_count - 1,
                    ),
                    byte_count=case(
                        (RATE_COUNTERS.c.byte_count <= byte_count, 0),
                        else_=RATE_COUNTERS.c.byte_count - byte_count,
                    ),
                )
            )

    def record_transfer(
        self,
        *,
        identity: TokenIdentity,
        operation: str,
        filename: str,
        byte_count: int,
        duration_ms: int,
        status: str,
        file_id: str | None = None,
        media_type: str | None = None,
        error_code: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        occurred_at = datetime.now(UTC)
        event = {
            "event_id": event_id,
            "token_id": identity.token_id,
            "token_prefix": identity.token_prefix,
            "operation": operation,
            "business_domain": identity.business_domain,
            "department": identity.department,
            "domain_shard": domain_shard(
                identity.business_domain, self.settings.paimon_domain_shards
            ),
            "ingest_date": occurred_at.date().isoformat(),
            "occurred_at": occurred_at.isoformat(),
            "file_id": file_id,
            "filename": filename,
            "media_type": media_type,
            "byte_count": byte_count,
            "duration_ms": duration_ms,
            "status": status,
            "error_code": error_code,
            "client_ip": client_ip,
            "user_agent": (user_agent or "")[:512],
            "claim_id": None,
            "claimed_at": None,
            "synced_at": None,
        }
        daily = {
            "stat_date": occurred_at.date().isoformat(),
            "token_id": identity.token_id,
            "token_prefix": identity.token_prefix,
            "business_domain": identity.business_domain,
            "department": identity.department,
            "operation": operation,
            "status": status,
            "request_count": 1,
            "byte_count": byte_count,
        }
        with self._lock, self._transaction(immediate=True) as connection:
            connection.execute(insert(TRANSFER_EVENTS).values(**event))
            connection.execute(self._upsert_daily_stats(daily))
        return event_id

    def pending_transfer_events(self, limit: int) -> list[dict[str, Any]]:
        with self._engine_required().connect() as connection:
            rows = (
                connection.execute(
                    select(TRANSFER_EVENTS)
                    .where(TRANSFER_EVENTS.c.synced_at.is_(None))
                    .order_by(TRANSFER_EVENTS.c.occurred_at.asc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def claim_transfer_events(
        self, limit: int, *, lease_seconds: int = 300
    ) -> tuple[str, list[dict[str, Any]]]:
        """Atomically lease an outbox batch to one API replica."""
        claim_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        stale = (now - timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._transaction(immediate=True) as connection:
            connection.execute(
                update(TRANSFER_EVENTS)
                .where(
                    and_(
                        TRANSFER_EVENTS.c.synced_at.is_(None),
                        TRANSFER_EVENTS.c.claimed_at.is_not(None),
                        TRANSFER_EVENTS.c.claimed_at < stale,
                    )
                )
                .values(claim_id=None, claimed_at=None)
            )
            statement = (
                select(TRANSFER_EVENTS.c.event_id)
                .where(
                    and_(
                        TRANSFER_EVENTS.c.synced_at.is_(None),
                        TRANSFER_EVENTS.c.claim_id.is_(None),
                    )
                )
                .order_by(TRANSFER_EVENTS.c.occurred_at.asc())
                .limit(limit)
            )
            if self.backend != "sqlite":
                statement = statement.with_for_update(skip_locked=True)
            event_ids = list(connection.execute(statement).scalars())
            if not event_ids:
                return claim_id, []
            connection.execute(
                update(TRANSFER_EVENTS)
                .where(TRANSFER_EVENTS.c.event_id.in_(event_ids))
                .values(claim_id=claim_id, claimed_at=now.isoformat())
            )
            rows = (
                connection.execute(
                    select(TRANSFER_EVENTS)
                    .where(TRANSFER_EVENTS.c.claim_id == claim_id)
                    .order_by(TRANSFER_EVENTS.c.occurred_at.asc())
                )
                .mappings()
                .all()
            )
        return claim_id, [dict(row) for row in rows]

    def mark_events_synced(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self._transaction() as connection:
            connection.execute(
                update(TRANSFER_EVENTS)
                .where(TRANSFER_EVENTS.c.event_id.in_(event_ids))
                .values(synced_at=datetime.now(UTC).isoformat(), claim_id=None, claimed_at=None)
            )

    def release_event_claim(self, claim_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                update(TRANSFER_EVENTS)
                .where(
                    and_(
                        TRANSFER_EVENTS.c.claim_id == claim_id,
                        TRANSFER_EVENTS.c.synced_at.is_(None),
                    )
                )
                .values(claim_id=None, claimed_at=None)
            )

    def transfer_stats(self, period: str) -> list[dict[str, Any]]:
        days = {"day": 1, "week": 7, "month": 31}.get(period)
        if days is None:
            raise MorphLakeError("invalid_period", "period must be day, week, or month")
        start = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
        statement = (
            select(
                TRANSFER_DAILY_STATS.c.token_prefix,
                TRANSFER_DAILY_STATS.c.business_domain,
                TRANSFER_DAILY_STATS.c.department,
                TRANSFER_DAILY_STATS.c.operation,
                TRANSFER_DAILY_STATS.c.status,
                func.sum(TRANSFER_DAILY_STATS.c.request_count).label("request_count"),
                func.sum(TRANSFER_DAILY_STATS.c.byte_count).label("byte_count"),
            )
            .where(TRANSFER_DAILY_STATS.c.stat_date >= start)
            .group_by(
                TRANSFER_DAILY_STATS.c.token_id,
                TRANSFER_DAILY_STATS.c.token_prefix,
                TRANSFER_DAILY_STATS.c.business_domain,
                TRANSFER_DAILY_STATS.c.department,
                TRANSFER_DAILY_STATS.c.operation,
                TRANSFER_DAILY_STATS.c.status,
            )
            .order_by(
                TRANSFER_DAILY_STATS.c.business_domain,
                TRANSFER_DAILY_STATS.c.department,
                TRANSFER_DAILY_STATS.c.token_prefix,
                TRANSFER_DAILY_STATS.c.operation,
                TRANSFER_DAILY_STATS.c.status,
            )
        )
        with self._engine_required().connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [dict(row) for row in rows]

    def recent_transfers(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._engine_required().connect() as connection:
            rows = (
                connection.execute(
                    select(TRANSFER_EVENTS)
                    .order_by(TRANSFER_EVENTS.c.occurred_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def unsynced_event_count(self) -> int:
        with self._engine_required().connect() as connection:
            value = connection.execute(
                select(func.count())
                .select_from(TRANSFER_EVENTS)
                .where(TRANSFER_EVENTS.c.synced_at.is_(None))
            ).scalar_one()
        return int(value)

    def cleanup_synced_events(self) -> int:
        cutoff = (
            datetime.now(UTC) - timedelta(days=self.settings.transfer_detail_retention_days)
        ).isoformat()
        with self._transaction() as connection:
            result = connection.execute(
                delete(TRANSFER_EVENTS).where(
                    and_(
                        TRANSFER_EVENTS.c.synced_at.is_not(None),
                        TRANSFER_EVENTS.c.occurred_at < cutoff,
                    )
                )
            )
        return result.rowcount

    def _initialize_schema(self) -> None:
        engine = self._engine_required()
        with engine.connect() as connection:
            if self.backend == "sqlite":
                connection.exec_driver_sql("BEGIN EXCLUSIVE")
                try:
                    self._create_and_seed(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                return

            self._acquire_schema_lock(connection)
            try:
                self._create_and_seed(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._release_schema_lock(connection)

    def _create_and_seed(self, connection: Connection) -> None:
        METADATA.create_all(connection, checkfirst=True)
        self._ensure_transfer_columns(connection)
        now = datetime.now(UTC).isoformat()
        values = {
            "schema_version": ("2", "MorphLake management schema version"),
            "initialized_at": (now, "First successful schema initialization time"),
            "database_backend": (self.backend, "Active management database backend"),
            "default_rate_period_seconds": (
                str(self.settings.default_rate_period_seconds),
                "Default token quota window in seconds",
            ),
            "default_upload_requests": (
                str(self.settings.default_upload_requests),
                "Default upload request quota",
            ),
            "default_download_requests": (
                str(self.settings.default_download_requests),
                "Default download request quota",
            ),
            "default_upload_bytes": (
                str(self.settings.default_upload_bytes),
                "Default upload byte quota",
            ),
            "default_download_bytes": (
                str(self.settings.default_download_bytes),
                "Default download byte quota",
            ),
        }
        for key, (value, description) in values.items():
            connection.execute(
                self._insert_do_nothing(
                    SYSTEM_CONFIG,
                    {
                        "config_key": key,
                        "config_value": value,
                        "description": description,
                        "updated_at": now,
                    },
                    ["config_key"],
                )
            )

    def _ensure_transfer_columns(self, connection: Connection) -> None:
        columns = {column["name"] for column in inspect(connection).get_columns("transfer_events")}
        for column in ("claim_id", "claimed_at"):
            if column not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE transfer_events ADD COLUMN {column} VARCHAR(40)"
                )

    def _acquire_schema_lock(self, connection: Connection) -> None:
        if self.backend == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": self._SCHEMA_LOCK_ID}
            )
        else:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:lock_name, 60)"),
                {"lock_name": self._SCHEMA_LOCK_NAME},
            ).scalar_one()
            if acquired != 1:
                raise TimeoutError("Timed out waiting for the management schema lock")
        connection.commit()

    def _release_schema_lock(self, connection: Connection) -> None:
        try:
            if self.backend == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": self._SCHEMA_LOCK_ID},
                )
            else:
                connection.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": self._SCHEMA_LOCK_NAME},
                )
            connection.commit()
        except Exception:
            connection.rollback()

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[Connection]:
        connection = self._engine_required().connect()
        try:
            if immediate and self.backend == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_do_nothing(self, table: Table, values: dict[str, Any], index_elements: list[str]):
        if self.backend == "sqlite":
            return (
                sqlite_insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=index_elements)
            )
        if self.backend == "postgresql":
            return (
                postgresql_insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=index_elements)
            )
        statement = mysql_insert(table).values(**values)
        first_key = index_elements[0]
        return statement.on_duplicate_key_update(
            **{first_key: getattr(statement.inserted, first_key)}
        )

    def _upsert_daily_stats(self, values: dict[str, Any]):
        index_elements = ["stat_date", "token_id", "operation", "status"]
        if self.backend == "sqlite":
            statement = sqlite_insert(TRANSFER_DAILY_STATS).values(**values)
            return statement.on_conflict_do_update(
                index_elements=index_elements,
                set_={
                    "request_count": TRANSFER_DAILY_STATS.c.request_count + 1,
                    "byte_count": TRANSFER_DAILY_STATS.c.byte_count + statement.excluded.byte_count,
                },
            )
        if self.backend == "postgresql":
            statement = postgresql_insert(TRANSFER_DAILY_STATS).values(**values)
            return statement.on_conflict_do_update(
                index_elements=index_elements,
                set_={
                    "request_count": TRANSFER_DAILY_STATS.c.request_count + 1,
                    "byte_count": TRANSFER_DAILY_STATS.c.byte_count + statement.excluded.byte_count,
                },
            )
        statement = mysql_insert(TRANSFER_DAILY_STATS).values(**values)
        return statement.on_duplicate_key_update(
            request_count=TRANSFER_DAILY_STATS.c.request_count + 1,
            byte_count=TRANSFER_DAILY_STATS.c.byte_count + statement.inserted.byte_count,
        )

    def _engine_required(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("AdminStore.initialize() must be called before use")
        return self._engine

    def _token_hash(self, plaintext: str) -> str:
        return hmac.new(
            self.settings.token_pepper.encode("utf-8"),
            plaintext.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _identity(row: Mapping[str, Any]) -> TokenIdentity:
        return TokenIdentity(
            token_id=row["token_id"],
            token_prefix=row["token_prefix"],
            business_domain=row["business_domain"],
            department=row["department"],
            assignee_name=row["assignee_name"],
            phone=row["phone"],
            status=row["status"],
            period_seconds=row["period_seconds"],
            upload_requests_limit=row["upload_requests_limit"],
            download_requests_limit=row["download_requests_limit"],
            upload_bytes_limit=row["upload_bytes_limit"],
            download_bytes_limit=row["download_bytes_limit"],
        )

    @staticmethod
    def _limits(identity: TokenIdentity, operation: str) -> tuple[int, int]:
        if operation == "upload":
            return identity.upload_requests_limit, identity.upload_bytes_limit
        if operation == "download":
            return identity.download_requests_limit, identity.download_bytes_limit
        raise ValueError(f"Unsupported transfer operation: {operation}")

    @staticmethod
    def _rate_error(operation: str, reset_at: int) -> MorphLakeError:
        retry_after = max(1, reset_at - int(time.time()))
        return MorphLakeError(
            "rate_limit_exceeded",
            f"{operation} rate limit exceeded; retry after {retry_after} seconds",
            429,
            headers={"Retry-After": str(retry_after)},
        )
