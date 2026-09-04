"""SQLite-backed token administration, rate limiting, and transfer outbox."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from morphlake.config import Settings
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


class AdminStore:
    """Small, transactional management database for a single service container."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = Path(settings.admin_db_path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    business_domain TEXT NOT NULL,
                    department TEXT NOT NULL,
                    assignee_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('active','disabled','deleted')),
                    allocated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    last_used_at TEXT,
                    period_seconds INTEGER NOT NULL,
                    upload_requests_limit INTEGER NOT NULL,
                    download_requests_limit INTEGER NOT NULL,
                    upload_bytes_limit INTEGER NOT NULL,
                    download_bytes_limit INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_tokens_scope
                    ON api_tokens (business_domain, department, status);

                CREATE TABLE IF NOT EXISTS rate_counters (
                    token_id TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK (operation IN ('upload','download')),
                    window_start INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    byte_count INTEGER NOT NULL,
                    PRIMARY KEY (token_id, operation, window_start)
                );

                CREATE TABLE IF NOT EXISTS transfer_events (
                    event_id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK (operation IN ('upload','download')),
                    business_domain TEXT NOT NULL,
                    department TEXT NOT NULL,
                    domain_shard INTEGER NOT NULL,
                    ingest_date TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    file_id TEXT,
                    filename TEXT NOT NULL,
                    media_type TEXT,
                    byte_count INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    client_ip TEXT,
                    user_agent TEXT,
                    claim_id TEXT,
                    claimed_at TEXT,
                    synced_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_transfer_events_unsynced
                    ON transfer_events (synced_at, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_transfer_events_scope_time
                    ON transfer_events (business_domain, department, occurred_at);

                CREATE TABLE IF NOT EXISTS transfer_daily_stats (
                    stat_date TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    business_domain TEXT NOT NULL,
                    department TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_count INTEGER NOT NULL,
                    byte_count INTEGER NOT NULL,
                    PRIMARY KEY (stat_date, token_id, operation, status)
                );
                """
            )
            self._ensure_transfer_columns(connection)

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
        random_value = secrets.token_urlsafe(32)
        token_prefix = secrets.token_hex(4)
        plaintext = f"mlk_{token_prefix}_{random_value}"
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
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO api_tokens (
                    token_id, token_hash, token_prefix, business_domain, department,
                    assignee_name, phone, notes, status, allocated_by, created_at,
                    updated_at, expires_at, period_seconds, upload_requests_limit,
                    download_requests_limit, upload_bytes_limit, download_bytes_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    self._token_hash(plaintext),
                    token_prefix,
                    identity.business_domain,
                    identity.department,
                    identity.assignee_name,
                    identity.phone,
                    notes.strip(),
                    allocated_by,
                    now,
                    now,
                    expires_at or None,
                    period_seconds,
                    upload_requests_limit,
                    download_requests_limit,
                    upload_bytes_limit,
                    download_bytes_limit,
                ),
            )
        return CreatedToken(identity=identity, plaintext=plaintext)

    def authenticate(self, plaintext: str | None) -> TokenIdentity:
        if not plaintext:
            raise MorphLakeError("token_required", "API token is required", 401)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE token_hash = ?", (self._token_hash(plaintext),)
            ).fetchone()
            if row is None:
                raise MorphLakeError("token_invalid", "API token is invalid", 401)
            if row["status"] == "disabled":
                raise MorphLakeError("token_disabled", "API token is disabled", 403)
            if row["status"] == "deleted":
                raise MorphLakeError("token_deleted", "API token has been deleted", 403)
            if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
                raise MorphLakeError("token_expired", "API token has expired", 403)
            connection.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE token_id = ?",
                (datetime.now(UTC).isoformat(), row["token_id"]),
            )
        return self._identity(row)

    def list_tokens(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        where = "" if include_deleted else "WHERE status != 'deleted'"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM api_tokens {where} ORDER BY created_at DESC"  # noqa: S608
            ).fetchall()
        return [dict(row) for row in rows]

    def set_token_status(self, token_id: str, status: str) -> None:
        if status not in {"active", "disabled", "deleted"}:
            raise MorphLakeError("invalid_token_status", "Unsupported token status")
        with self._connect() as connection:
            deleted_guard = "" if status == "deleted" else "AND status != 'deleted'"
            result = connection.execute(
                f"UPDATE api_tokens SET status = ?, updated_at = ? "  # noqa: S608
                f"WHERE token_id = ? {deleted_guard}",
                (status, datetime.now(UTC).isoformat(), token_id),
            )
            if result.rowcount != 1:
                existing = connection.execute(
                    "SELECT status FROM api_tokens WHERE token_id = ?", (token_id,)
                ).fetchone()
                if existing and existing["status"] == "deleted":
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
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE api_tokens SET period_seconds = ?, upload_requests_limit = ?,
                    download_requests_limit = ?, upload_bytes_limit = ?,
                    download_bytes_limit = ?, updated_at = ?
                WHERE token_id = ? AND status != 'deleted'
                """,
                (
                    period_seconds,
                    upload_requests_limit,
                    download_requests_limit,
                    upload_bytes_limit,
                    download_bytes_limit,
                    datetime.now(UTC).isoformat(),
                    token_id,
                ),
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
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM rate_counters WHERE token_id = ? AND window_start < ?",
                (
                    identity.token_id,
                    now_epoch - max(86_400, identity.period_seconds * 2),
                ),
            )
            row = connection.execute(
                """
                SELECT request_count, byte_count FROM rate_counters
                WHERE token_id = ? AND operation = ? AND window_start = ?
                """,
                (identity.token_id, operation, window_start),
            ).fetchone()
            requests = int(row["request_count"]) if row else 0
            used_bytes = int(row["byte_count"]) if row else 0
            if request_limit and requests + 1 > request_limit:
                raise self._rate_error(operation, reset_at)
            if byte_limit and used_bytes + byte_count > byte_limit:
                raise self._rate_error(operation, reset_at)
            connection.execute(
                """
                INSERT INTO rate_counters
                    (token_id, operation, window_start, request_count, byte_count)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(token_id, operation, window_start) DO UPDATE SET
                    request_count = request_count + 1,
                    byte_count = byte_count + excluded.byte_count
                """,
                (identity.token_id, operation, window_start, byte_count),
            )
        return RateDecision(
            remaining_requests=None if not request_limit else request_limit - requests - 1,
            remaining_bytes=None if not byte_limit else byte_limit - used_bytes - byte_count,
            reset_at_epoch=reset_at,
        )

    def refund_rate(self, identity: TokenIdentity, operation: str, byte_count: int) -> None:
        now_epoch = int(time.time())
        window_start = now_epoch - now_epoch % identity.period_seconds
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE rate_counters SET
                    request_count = MAX(0, request_count - 1),
                    byte_count = MAX(0, byte_count - ?)
                WHERE token_id = ? AND operation = ? AND window_start = ?
                """,
                (byte_count, identity.token_id, operation, window_start),
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
        event = (
            event_id,
            identity.token_id,
            identity.token_prefix,
            operation,
            identity.business_domain,
            identity.department,
            domain_shard(identity.business_domain, self.settings.paimon_domain_shards),
            occurred_at.date().isoformat(),
            occurred_at.isoformat(),
            file_id,
            filename,
            media_type,
            byte_count,
            duration_ms,
            status,
            error_code,
            client_ip,
            (user_agent or "")[:512],
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO transfer_events (
                    event_id, token_id, token_prefix, operation, business_domain,
                    department, domain_shard, ingest_date, occurred_at, file_id,
                    filename, media_type, byte_count, duration_ms, status, error_code,
                    client_ip, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event,
            )
            connection.execute(
                """
                INSERT INTO transfer_daily_stats (
                    stat_date, token_id, token_prefix, business_domain, department,
                    operation, status, request_count, byte_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(stat_date, token_id, operation, status) DO UPDATE SET
                    request_count = request_count + 1,
                    byte_count = byte_count + excluded.byte_count
                """,
                (
                    occurred_at.date().isoformat(),
                    identity.token_id,
                    identity.token_prefix,
                    identity.business_domain,
                    identity.department,
                    operation,
                    status,
                    byte_count,
                ),
            )
        return event_id

    def pending_transfer_events(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transfer_events WHERE synced_at IS NULL
                ORDER BY occurred_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_transfer_events(
        self, limit: int, *, lease_seconds: int = 300
    ) -> tuple[str, list[dict[str, Any]]]:
        """Atomically lease an outbox batch to one API replica."""
        claim_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        stale = (now - timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE transfer_events SET claim_id = NULL, claimed_at = NULL
                WHERE synced_at IS NULL AND claimed_at < ?
                """,
                (stale,),
            )
            rows = connection.execute(
                """
                SELECT event_id FROM transfer_events
                WHERE synced_at IS NULL AND claim_id IS NULL
                ORDER BY occurred_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            event_ids = [row["event_id"] for row in rows]
            if not event_ids:
                return claim_id, []
            placeholders = ",".join("?" for _ in event_ids)
            connection.execute(
                f"UPDATE transfer_events SET claim_id = ?, claimed_at = ? "  # noqa: S608
                f"WHERE event_id IN ({placeholders})",
                (claim_id, now.isoformat(), *event_ids),
            )
            claimed = connection.execute(
                "SELECT * FROM transfer_events WHERE claim_id = ? ORDER BY occurred_at",
                (claim_id,),
            ).fetchall()
        return claim_id, [dict(row) for row in claimed]

    def mark_events_synced(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE transfer_events SET synced_at = ? "  # noqa: S608
                f"WHERE event_id IN ({placeholders})",
                (datetime.now(UTC).isoformat(), *event_ids),
            )

    def release_event_claim(self, claim_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE transfer_events SET claim_id = NULL, claimed_at = NULL
                WHERE claim_id = ? AND synced_at IS NULL
                """,
                (claim_id,),
            )

    def transfer_stats(self, period: str) -> list[dict[str, Any]]:
        days = {"day": 1, "week": 7, "month": 31}.get(period)
        if days is None:
            raise MorphLakeError("invalid_period", "period must be day, week, or month")
        start = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT token_prefix, business_domain, department, operation, status,
                       SUM(request_count) AS request_count,
                       SUM(byte_count) AS byte_count
                FROM transfer_daily_stats WHERE stat_date >= ?
                GROUP BY token_id, operation, status
                ORDER BY business_domain, department, token_prefix, operation, status
                """,
                (start,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_transfers(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transfer_events ORDER BY occurred_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def unsynced_event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM transfer_events WHERE synced_at IS NULL"
            ).fetchone()
        return int(row["count"])

    def cleanup_synced_events(self) -> int:
        cutoff = (
            datetime.now(UTC) - timedelta(days=self.settings.transfer_sqlite_retention_days)
        ).isoformat()
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM transfer_events WHERE synced_at IS NOT NULL AND occurred_at < ?",
                (cutoff,),
            )
        return result.rowcount

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _ensure_transfer_columns(connection: sqlite3.Connection) -> None:
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(transfer_events)")}
        for column in ("claim_id", "claimed_at"):
            if column not in existing:
                connection.execute(f"ALTER TABLE transfer_events ADD COLUMN {column} TEXT")

    def _token_hash(self, plaintext: str) -> str:
        return hmac.new(
            self.settings.token_pepper.encode("utf-8"),
            plaintext.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _identity(row: sqlite3.Row) -> TokenIdentity:
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
