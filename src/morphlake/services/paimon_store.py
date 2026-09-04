"""PyPaimon 2.0 storage and native-search adapter."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import pyarrow as pa
from pypaimon.common.predicate_builder import PredicateBuilder
from pypaimon.multimodal import connect

from morphlake.config import Settings
from morphlake.errors import ConfigurationError, NotFoundError, StorageError
from morphlake.partitioning import domain_shard

ASSET_TABLE = "asset"
TEXT_TABLE = "text"
IMAGE_TABLE = "image"
AUDIO_TABLE = "audio"
AUDIT_TABLE = "audit"
PARTITION_KEYS = ["ingest_date", "domain_shard"]
PUBLIC_COLUMNS = [
    "file_id",
    "filename",
    "media_type",
    "content_type",
    "file_size",
    "business_domain",
    "department",
    "created_at",
    "object_bucket",
    "object_key",
    "object_etag",
    "chunk_count",
]
SEARCH_COLUMNS = [
    "file_id",
    "filename",
    "media_type",
    "business_domain",
    "department",
    "created_at",
    "record_type",
    "chunk_index",
    "content_text",
]


def asset_schema(_: Settings) -> pa.Schema:
    return pa.schema(
        [
            pa.field("file_id", pa.string(), nullable=False),
            pa.field("business_domain", pa.string(), nullable=False),
            pa.field("department", pa.string(), nullable=False),
            pa.field("domain_shard", pa.int32(), nullable=False),
            pa.field("ingest_date", pa.string(), nullable=False),
            pa.field("created_at", pa.string(), nullable=False),
            pa.field("filename", pa.string(), nullable=False),
            pa.field("media_type", pa.string(), nullable=False),
            pa.field("content_type", pa.string(), nullable=False),
            pa.field("file_size", pa.int64(), nullable=False),
            pa.field("content_sha256", pa.string(), nullable=False),
            pa.field("object_bucket", pa.string(), nullable=False),
            pa.field("object_key", pa.string(), nullable=False),
            pa.field("object_etag", pa.string()),
            pa.field("chunk_count", pa.int32(), nullable=False),
        ]
    )


def text_schema(settings: Settings) -> pa.Schema:
    return pa.schema(
        [
            *_search_fields("segment_id"),
            pa.field("segment_type", pa.string(), nullable=False),
            pa.field("chunk_start", pa.int64()),
            pa.field("chunk_end", pa.int64()),
            pa.field("embedding_model", pa.string(), nullable=False),
            pa.field("embedding_version", pa.string(), nullable=False),
            pa.field(
                "text_embedding",
                pa.list_(pa.float32(), settings.text_vector_dimension),
                nullable=False,
            ),
        ]
    )


def image_schema(settings: Settings) -> pa.Schema:
    return pa.schema(
        [
            *_search_fields("feature_id"),
            pa.field("feature_type", pa.string(), nullable=False),
            pa.field("embedding_model", pa.string(), nullable=False),
            pa.field("embedding_version", pa.string(), nullable=False),
            pa.field(
                "image_embedding",
                pa.list_(pa.float32(), settings.image_vector_dimension),
                nullable=False,
            ),
        ]
    )


def audio_schema(settings: Settings) -> pa.Schema:
    return pa.schema(
        [
            *_search_fields("feature_id"),
            pa.field("feature_type", pa.string(), nullable=False),
            pa.field("start_ms", pa.int64()),
            pa.field("end_ms", pa.int64()),
            pa.field("embedding_model", pa.string(), nullable=False),
            pa.field("embedding_version", pa.string(), nullable=False),
            pa.field(
                "audio_embedding",
                pa.list_(pa.float32(), settings.audio_vector_dimension),
                nullable=False,
            ),
        ]
    )


def audit_schema(_: Settings) -> pa.Schema:
    """Append-only upload/download audit records retained in Paimon."""
    return pa.schema(
        [
            pa.field("event_id", pa.string(), nullable=False),
            pa.field("token_id", pa.string(), nullable=False),
            pa.field("token_prefix", pa.string(), nullable=False),
            pa.field("operation", pa.string(), nullable=False),
            pa.field("business_domain", pa.string(), nullable=False),
            pa.field("department", pa.string(), nullable=False),
            pa.field("domain_shard", pa.int32(), nullable=False),
            pa.field("ingest_date", pa.string(), nullable=False),
            pa.field("occurred_at", pa.string(), nullable=False),
            pa.field("file_id", pa.string()),
            pa.field("filename", pa.string(), nullable=False),
            pa.field("media_type", pa.string()),
            pa.field("byte_count", pa.int64(), nullable=False),
            pa.field("duration_ms", pa.int64(), nullable=False),
            pa.field("status", pa.string(), nullable=False),
            pa.field("error_code", pa.string()),
            pa.field("client_ip", pa.string()),
            pa.field("user_agent", pa.string()),
        ]
    )


def _search_fields(identifier: str) -> list[pa.Field]:
    return [
        pa.field(identifier, pa.string(), nullable=False),
        pa.field("file_id", pa.string(), nullable=False),
        pa.field("business_domain", pa.string(), nullable=False),
        pa.field("department", pa.string(), nullable=False),
        pa.field("domain_shard", pa.int32(), nullable=False),
        pa.field("ingest_date", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("filename", pa.string(), nullable=False),
        pa.field("media_type", pa.string(), nullable=False),
        pa.field("record_type", pa.string(), nullable=False),
        pa.field("chunk_index", pa.int32()),
        pa.field("content_text", pa.string()),
    ]


class PaimonStore:
    """Owns five append-only Paimon tables with one partition strategy."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.connection = connect(
            database=settings.paimon_database,
            options=settings.paimon_catalog_options(),
        )
        self.tables: dict[str, Any] = {}
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """Create and validate the complete table model on service startup."""
        definitions = {
            ASSET_TABLE: (self.settings.paimon_table, asset_schema(self.settings), False),
            TEXT_TABLE: (self.settings.paimon_text_table, text_schema(self.settings), True),
            IMAGE_TABLE: (self.settings.paimon_image_table, image_schema(self.settings), True),
            AUDIO_TABLE: (self.settings.paimon_audio_table, audio_schema(self.settings), True),
            AUDIT_TABLE: (self.settings.paimon_audit_table, audit_schema(self.settings), False),
        }
        try:
            for key, (name, schema, has_vector) in definitions.items():
                self.tables[key] = self.connection.create_table(
                    name,
                    schema=schema,
                    options=self._table_options(has_vector=has_vector),
                    partitioned=PARTITION_KEYS,
                    ignore_if_exists=True,
                )
                self._validate_existing_table(key, schema)
        except (ConfigurationError, ValueError):
            raise
        except Exception as exc:
            raise StorageError(f"Paimon initialization failed: {exc}") from exc

    def add(
        self,
        *,
        asset: dict[str, Any],
        text_segments: list[dict[str, Any]],
        image_features: list[dict[str, Any]],
        audio_features: list[dict[str, Any]],
    ) -> None:
        """Write features first, then publish the asset descriptor last."""
        writes = (
            (TEXT_TABLE, text_segments, text_schema(self.settings)),
            (IMAGE_TABLE, image_features, image_schema(self.settings)),
            (AUDIO_TABLE, audio_features, audio_schema(self.settings)),
            (ASSET_TABLE, [asset], asset_schema(self.settings)),
        )
        try:
            with self._lock:
                for table_key, rows, schema in writes:
                    if rows:
                        arrow = pa.Table.from_pylist(rows, schema=schema)
                        self._require_table(table_key).add(arrow)
        except Exception as exc:
            raise StorageError(f"Paimon write failed: {exc}") from exc

    def maintain_indexes(self) -> None:
        """Incrementally build native indexes for newly committed row ranges."""
        try:
            with self._lock:
                self._build_scalar_indexes(
                    ASSET_TABLE,
                    ["file_id"],
                    ["business_domain", "department", "media_type"],
                )
                self._build_scalar_indexes(
                    TEXT_TABLE,
                    ["file_id"],
                    ["business_domain", "department", "media_type", "segment_type"],
                )
                self._raw_table(TEXT_TABLE).create_global_index("content_text", "full-text")
                self._build_vector_index(TEXT_TABLE, "text_embedding")
                self._build_scalar_indexes(
                    IMAGE_TABLE, ["file_id"], ["business_domain", "department"]
                )
                self._build_vector_index(IMAGE_TABLE, "image_embedding")
                self._build_scalar_indexes(
                    AUDIO_TABLE, ["file_id"], ["business_domain", "department"]
                )
                self._build_vector_index(AUDIO_TABLE, "audio_embedding")
                self._build_scalar_indexes(
                    AUDIT_TABLE,
                    ["event_id", "token_id", "file_id"],
                    ["business_domain", "department", "operation", "status"],
                )
                self._refresh_tables()
        except Exception as exc:
            raise StorageError(f"Paimon index maintenance failed: {exc}") from exc

    def add_transfer_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        columns = audit_schema(self.settings).names
        rows = [{column: event.get(column) for column in columns} for event in events]
        try:
            with self._lock:
                self._require_table(AUDIT_TABLE).add(
                    pa.Table.from_pylist(rows, schema=audit_schema(self.settings))
                )
        except Exception as exc:
            raise StorageError(f"Paimon transfer audit write failed: {exc}") from exc

    def list_assets(
        self,
        *,
        media_type: str | None,
        business_domain: str | None,
        department: str | None,
        filename: str | None,
        start_date: date | None,
        end_date: date | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        builder = self._builder(ASSET_TABLE)
        predicates = []
        if media_type:
            predicates.append(builder.equal("media_type", media_type))
        if business_domain:
            predicates.extend(self._domain_predicates(builder, business_domain))
        if department:
            predicates.append(builder.equal("department", department))
        if filename:
            predicates.append(builder.contains("filename", filename))
        predicates.extend(self._date_predicates(builder, start_date, end_date))
        rows = self._read(
            ASSET_TABLE,
            PredicateBuilder.and_predicates(predicates),
            columns=PUBLIC_COLUMNS,
            limit=None,
        )
        rows.sort(key=lambda row: (row["created_at"], row["file_id"]), reverse=True)
        return rows[offset : offset + limit]

    def get_asset(self, file_id: str) -> dict[str, Any]:
        builder = self._builder(ASSET_TABLE)
        rows = self._read(
            ASSET_TABLE,
            builder.equal("file_id", file_id),
            columns=PUBLIC_COLUMNS,
            limit=1,
        )
        if not rows:
            raise NotFoundError(f"File {file_id} does not exist")
        return rows[0]

    def full_text_search(
        self,
        *,
        business_domain: str,
        department: str | None,
        keyword: str,
        start_date: date | None,
        end_date: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        partition_predicate = self._partition_predicate(
            TEXT_TABLE, business_domain, start_date, end_date
        )
        exact_predicate = self._search_predicate(
            TEXT_TABLE, business_domain, department, start_date, end_date
        )
        fetch_limit = max(limit * 5, 50)
        try:
            search = (
                self._raw_table(TEXT_TABLE)
                .new_full_text_search_builder()
                .with_query(
                    "content_text",
                    json.dumps({"match": {"query": keyword}}, separators=(",", ":")),
                )
                .with_limit(fetch_limit)
                .with_partition_filter(partition_predicate)
            )
            result = search.execute_local()
            rows = self._read(
                TEXT_TABLE,
                exact_predicate,
                columns=[*SEARCH_COLUMNS, "_ROW_ID"],
                limit=fetch_limit,
                global_index_result=result,
            )
            return self._committed_hits(self._rank_hits(rows, result), limit)
        except Exception as exc:
            raise StorageError(f"Paimon full-text search failed: {exc}") from exc

    def vector_search(
        self,
        *,
        business_domain: str,
        department: str | None,
        vector: list[float],
        vector_field: str,
        start_date: date | None,
        end_date: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_key, column, expected = {
            "text": (TEXT_TABLE, "text_embedding", self.settings.text_vector_dimension),
            "image": (IMAGE_TABLE, "image_embedding", self.settings.image_vector_dimension),
            "audio": (AUDIO_TABLE, "audio_embedding", self.settings.audio_vector_dimension),
        }[vector_field]
        if len(vector) != expected:
            raise ConfigurationError(
                f"Vector dimension for {vector_field} must be {expected}, got {len(vector)}"
            )
        exact_predicate = self._search_predicate(
            table_key, business_domain, department, start_date, end_date
        )
        partition_predicate = self._partition_predicate(
            table_key, business_domain, start_date, end_date
        )
        fetch_limit = max(limit * 5, 50)
        try:
            result = (
                self._raw_table(table_key)
                .new_vector_search_builder()
                .with_vector_column(column)
                .with_query_vector(vector)
                .with_limit(fetch_limit)
                .with_filter(exact_predicate)
                .with_partition_filter(partition_predicate)
                .execute_local()
            )
            rows = self._read(
                table_key,
                exact_predicate,
                columns=[*SEARCH_COLUMNS, "_ROW_ID"],
                limit=fetch_limit,
                global_index_result=result,
            )
            return self._committed_hits(self._rank_hits(rows, result), limit)
        except Exception as exc:
            raise StorageError(f"Paimon vector search failed: {exc}") from exc

    def ping(self) -> None:
        for key in (ASSET_TABLE, TEXT_TABLE, IMAGE_TABLE, AUDIO_TABLE, AUDIT_TABLE):
            self._require_table(key)
        self.connection.catalog.list_tables(self.settings.paimon_database)

    def _table_options(self, *, has_vector: bool) -> dict[str, str]:
        options = {
            "bucket": "-1",
            "deletion-vectors.enabled": "false",
            "data-evolution.enabled": "true",
            "row-tracking.enabled": "true",
            "blob-as-descriptor": "true",
            "file.compression": "zstd",
            "target-file-size": "512 mb",
            "global-index.row-count-per-shard": str(self.settings.paimon_index_row_count_per_shard),
            "scalar-index.search-mode": "fast",
            "full-text-index.search-mode": "fast",
            "vector-index.search-mode": "fast",
            "snapshot.time-retained": "72 h",
            "snapshot.num-retained.min": "10",
        }
        if has_vector:
            options.update({"vector.file.format": "vortex", "vector.target-file-size": "1 gb"})
        return options

    def _build_scalar_indexes(
        self, table_key: str, btree_columns: list[str], bitmap_columns: list[str]
    ) -> None:
        raw = self._raw_table(table_key)
        for column in btree_columns:
            raw.create_global_index(column, "btree")
        for column in bitmap_columns:
            raw.create_global_index(column, "bitmap")

    def _build_vector_index(self, table_key: str, column: str) -> None:
        self._raw_table(table_key).create_global_index(
            column, self.settings.paimon_vector_index_type
        )

    def _validate_existing_table(self, table_key: str, schema: pa.Schema) -> None:
        raw = self._raw_table(table_key)
        actual_fields = {field.name: field for field in raw.fields}
        missing = sorted(set(schema.names) - set(actual_fields))
        if missing:
            raise ConfigurationError(
                f"Existing Paimon table {table_key} is missing columns: {missing}"
            )
        for field in schema:
            if pa.types.is_fixed_size_list(field.type):
                actual_dimension = getattr(actual_fields[field.name].type, "length", None)
                if actual_dimension != field.type.list_size:
                    raise ConfigurationError(
                        f"Existing table {table_key}.{field.name} dimension must be "
                        f"{field.type.list_size}, got {actual_dimension}"
                    )
        if list(raw.partition_keys) != PARTITION_KEYS:
            raise ConfigurationError(
                f"Existing table {table_key} partitions must be {PARTITION_KEYS}, "
                f"got {raw.partition_keys}"
            )
        options = raw.table_schema.options
        if options.get("bucket") != "-1":
            raise ConfigurationError(
                f"Existing table {table_key} must use bucket=-1 for global indexes"
            )
        if options.get("deletion-vectors.enabled", "false").lower() != "false":
            raise ConfigurationError(f"Existing table {table_key} must disable deletion vectors")

    def _committed_hits(self, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        file_ids = list(dict.fromkeys(row["file_id"] for row in rows))
        if not file_ids:
            return []
        builder = self._builder(ASSET_TABLE)
        committed = self._read(
            ASSET_TABLE,
            builder.is_in("file_id", file_ids),
            columns=["file_id"],
            limit=len(file_ids),
        )
        committed_ids = {row["file_id"] for row in committed}
        return [row for row in rows if row["file_id"] in committed_ids][:limit]

    def _read(
        self,
        table_key: str,
        predicate,
        *,
        columns: list[str],
        limit: int | None,
        global_index_result=None,
    ) -> list[dict[str, Any]]:
        raw = self._raw_table(table_key)
        read_builder = raw.new_read_builder().with_projection(columns)
        if limit is not None:
            read_builder = read_builder.with_limit(limit)
        if predicate is not None:
            read_builder = read_builder.with_filter(predicate)
        scan = read_builder.new_scan()
        if global_index_result is not None:
            scan = scan.with_global_index_result(global_index_result)
        plan = scan.plan()
        return read_builder.new_read().to_arrow(plan.splits()).to_pylist()

    def _search_predicate(
        self,
        table_key: str,
        business_domain: str,
        department: str | None,
        start_date: date | None,
        end_date: date | None,
    ):
        builder = self._builder(table_key)
        predicates = self._domain_predicates(builder, business_domain)
        if department:
            predicates.append(builder.equal("department", department))
        predicates.extend(self._date_predicates(builder, start_date, end_date))
        return PredicateBuilder.and_predicates(predicates)

    def _partition_predicate(
        self,
        table_key: str,
        business_domain: str,
        start_date: date | None,
        end_date: date | None,
    ):
        raw = self._raw_table(table_key)
        builder = PredicateBuilder(raw.partition_keys_fields)
        predicates = [
            builder.equal(
                "domain_shard",
                domain_shard(business_domain, self.settings.paimon_domain_shards),
            )
        ]
        if start_date:
            predicates.append(builder.greater_or_equal("ingest_date", start_date.isoformat()))
        if end_date:
            predicates.append(builder.less_or_equal("ingest_date", end_date.isoformat()))
        return PredicateBuilder.and_predicates(predicates)

    def _domain_predicates(self, builder: PredicateBuilder, business_domain: str) -> list[Any]:
        return [
            builder.equal("business_domain", business_domain),
            builder.equal(
                "domain_shard",
                domain_shard(business_domain, self.settings.paimon_domain_shards),
            ),
        ]

    @staticmethod
    def _date_predicates(
        builder: PredicateBuilder, start_date: date | None, end_date: date | None
    ) -> Iterable:
        values = []
        if start_date:
            values.append(builder.greater_or_equal("created_at", start_date.isoformat()))
        if end_date:
            values.append(
                builder.less_than("created_at", (end_date + timedelta(days=1)).isoformat())
            )
        return values

    def _builder(self, table_key: str) -> PredicateBuilder:
        return PredicateBuilder(self._raw_table(table_key).fields)

    def _raw_table(self, table_key: str):
        return self._require_table(table_key).raw_table

    def _require_table(self, table_key: str):
        try:
            return self.tables[table_key]
        except KeyError as exc:
            raise StorageError(f"Paimon table {table_key} is not initialized") from exc

    def _refresh_tables(self) -> None:
        """Reload table objects so readers observe snapshots created during maintenance."""
        names = {
            ASSET_TABLE: self.settings.paimon_table,
            TEXT_TABLE: self.settings.paimon_text_table,
            IMAGE_TABLE: self.settings.paimon_image_table,
            AUDIO_TABLE: self.settings.paimon_audio_table,
            AUDIT_TABLE: self.settings.paimon_audit_table,
        }
        self.tables.update({key: self.connection.get_table(name) for key, name in names.items()})

    @staticmethod
    def _rank_hits(rows: list[dict[str, Any]], result: Any) -> list[dict[str, Any]]:
        """Restore score order lost when Paimon's bitmap result is materialized."""
        score_getter = getattr(result, "score_getter", lambda: None)()
        if score_getter is not None:

            def score(row: dict[str, Any]) -> float:
                value = score_getter(row.get("_ROW_ID"))
                return float(value) if value is not None else float("-inf")

            rows.sort(key=score, reverse=True)
        for row in rows:
            row.pop("_ROW_ID", None)
        return rows
