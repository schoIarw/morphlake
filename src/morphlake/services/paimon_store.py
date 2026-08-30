"""PyPaimon 2.0 metadata and native search adapter."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import pyarrow as pa
from pypaimon.common.predicate_builder import PredicateBuilder
from pypaimon.multimodal import connect

from morphlake.config import Settings
from morphlake.errors import ConfigurationError, NotFoundError, StorageError

LOGGER = logging.getLogger(__name__)

PARTITION_KEYS = ["business_domain", "department", "upload_month"]
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


def table_schema(settings: Settings) -> pa.Schema:
    return pa.schema(
        [
            pa.field("row_id", pa.string(), nullable=False),
            pa.field("file_id", pa.string(), nullable=False),
            pa.field("record_type", pa.string(), nullable=False),
            pa.field("business_domain", pa.string(), nullable=False),
            pa.field("department", pa.string(), nullable=False),
            pa.field("upload_month", pa.string(), nullable=False),
            pa.field("created_at", pa.string(), nullable=False),
            pa.field("filename", pa.string(), nullable=False),
            pa.field("media_type", pa.string(), nullable=False),
            pa.field("content_type", pa.string(), nullable=False),
            pa.field("file_size", pa.int64(), nullable=False),
            pa.field("object_bucket", pa.string(), nullable=False),
            pa.field("object_key", pa.string(), nullable=False),
            pa.field("object_etag", pa.string()),
            pa.field("chunk_count", pa.int32(), nullable=False),
            pa.field("chunk_index", pa.int32()),
            pa.field("chunk_start", pa.int64()),
            pa.field("chunk_end", pa.int64()),
            pa.field("content_text", pa.string()),
            pa.field("text_embedding", pa.list_(pa.float32())),
            pa.field("image_embedding", pa.list_(pa.float32())),
            pa.field("audio_embedding", pa.list_(pa.float32())),
        ]
    )


class PaimonStore:
    """Owns one append-only, partitioned multimodal Paimon table."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.connection = connect(
            database=settings.paimon_database,
            options=settings.paimon_catalog_options(),
        )
        self.table = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        options = {
            # PyPaimon 2.0 generic global indexes require unaware buckets and
            # reject deletion vectors. Partitioning bounds each search domain.
            "bucket": "-1",
            "deletion-vectors.enabled": "false",
            "data-evolution.enabled": "true",
            "row-tracking.enabled": "true",
            "blob-as-descriptor": "true",
            "global-index.search-mode": "full",
        }
        try:
            self.table = self.connection.create_table(
                self.settings.paimon_table,
                schema=table_schema(self.settings),
                options=options,
                partitioned=PARTITION_KEYS,
                ignore_if_exists=True,
            )
            self._validate_existing_table()
        except (ConfigurationError, ValueError):
            raise
        except Exception as exc:
            raise StorageError(f"Paimon initialization failed: {exc}") from exc

    def add(self, records: list[dict[str, Any]]) -> None:
        table = self._require_table()
        arrow = pa.Table.from_pylist(records, schema=table_schema(self.settings))
        try:
            with self._lock:
                table.add(arrow)
                self._build_indexes()
        except Exception as exc:
            raise StorageError(f"Paimon write or index build failed: {exc}") from exc

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
        builder = self._builder()
        predicates = [builder.equal("record_type", "file")]
        if media_type:
            predicates.append(builder.equal("media_type", media_type))
        if business_domain:
            predicates.append(builder.equal("business_domain", business_domain))
        if department:
            predicates.append(builder.equal("department", department))
        if filename:
            predicates.append(builder.contains("filename", filename))
        predicates.extend(self._date_predicates(builder, start_date, end_date))
        rows = self._read(
            PredicateBuilder.and_predicates(predicates),
            columns=PUBLIC_COLUMNS,
            limit=limit + offset,
        )
        rows.sort(key=lambda row: (row["created_at"], row["file_id"]), reverse=True)
        return rows[offset : offset + limit]

    def get_asset(self, file_id: str) -> dict[str, Any]:
        builder = self._builder()
        predicate = PredicateBuilder.and_predicates(
            [builder.equal("record_type", "file"), builder.equal("file_id", file_id)]
        )
        rows = self._read(predicate, columns=PUBLIC_COLUMNS, limit=1)
        if not rows:
            raise NotFoundError(f"File {file_id} does not exist")
        return rows[0]

    def full_text_search(
        self,
        *,
        business_domain: str,
        keyword: str,
        start_date: date | None,
        end_date: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        raw = self._raw_table()
        partition_predicate = self._partition_predicate(business_domain, None, start_date, end_date)
        exact_predicate = self._search_predicate(business_domain, start_date, end_date)
        fetch_limit = max(limit * 10, 1000)
        try:
            search = (
                raw.new_full_text_search_builder()
                .with_query(
                    "content_text",
                    json.dumps({"match": {"query": keyword}}, separators=(",", ":")),
                )
                .with_limit(fetch_limit)
            )
            if partition_predicate is not None:
                search = search.with_partition_filter(partition_predicate)
            result = search.execute_local()
            return self._read(
                exact_predicate,
                columns=SEARCH_COLUMNS,
                limit=limit,
                global_index_result=result,
            )
        except Exception as exc:
            raise StorageError(f"Paimon full-text search failed: {exc}") from exc

    def vector_search(
        self,
        *,
        business_domain: str,
        vector: list[float],
        vector_field: str,
        start_date: date | None,
        end_date: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        column = f"{vector_field}_embedding"
        expected = {
            "text": self.settings.text_vector_dimension,
            "image": self.settings.image_vector_dimension,
            "audio": self.settings.audio_vector_dimension,
        }[vector_field]
        if len(vector) != expected:
            raise ConfigurationError(
                f"Vector dimension for {vector_field} must be {expected}, got {len(vector)}"
            )
        raw = self._raw_table()
        exact_predicate = self._search_predicate(business_domain, start_date, end_date)
        try:
            search = (
                raw.new_vector_search_builder()
                .with_vector_column(column)
                .with_query_vector(vector)
                .with_limit(limit)
                .with_filter(exact_predicate)
            )
            result = search.execute_local()
            return self._read(
                exact_predicate,
                columns=SEARCH_COLUMNS,
                limit=limit,
                global_index_result=result,
            )
        except Exception as exc:
            raise StorageError(f"Paimon vector search failed: {exc}") from exc

    def ping(self) -> None:
        self._require_table()
        self.connection.catalog.list_tables(self.settings.paimon_database)

    def _build_indexes(self) -> None:
        table = self._require_table()
        raw = table.raw_table
        # These builds are incremental: PyPaimon skips already indexed row ranges.
        raw.create_global_index("file_id", "btree")
        raw.create_global_index("content_text", "full-text")
        raw.create_global_index(
            "text_embedding",
            "ivf-flat",
            options={"ivf-flat.dimension": str(self.settings.text_vector_dimension)},
        )
        raw.create_global_index(
            "image_embedding",
            "ivf-flat",
            options={"ivf-flat.dimension": str(self.settings.image_vector_dimension)},
        )
        raw.create_global_index(
            "audio_embedding",
            "ivf-flat",
            options={"ivf-flat.dimension": str(self.settings.audio_vector_dimension)},
        )

    def _validate_existing_table(self) -> None:
        raw = self._raw_table()
        actual_fields = {field.name for field in raw.fields}
        required_fields = set(table_schema(self.settings).names)
        missing = sorted(required_fields - actual_fields)
        if missing:
            raise ConfigurationError(f"Existing Paimon table is missing columns: {missing}")
        if list(raw.partition_keys) != PARTITION_KEYS:
            raise ConfigurationError(
                f"Existing table partitions must be {PARTITION_KEYS}, got {raw.partition_keys}"
            )
        options = raw.table_schema.options
        if options.get("bucket") != "-1":
            raise ConfigurationError("Existing table must use bucket=-1 for generic global indexes")
        if options.get("deletion-vectors.enabled", "false").lower() != "false":
            raise ConfigurationError("Existing table must disable deletion vectors")

    def _read(
        self,
        predicate,
        *,
        columns: list[str],
        limit: int,
        global_index_result=None,
    ) -> list[dict[str, Any]]:
        raw = self._raw_table()
        read_builder = raw.new_read_builder().with_projection(columns).with_limit(limit)
        if predicate is not None:
            read_builder = read_builder.with_filter(predicate)
        scan = read_builder.new_scan()
        if global_index_result is not None:
            scan = scan.with_global_index_result(global_index_result)
        plan = scan.plan()
        return read_builder.new_read().to_arrow(plan.splits()).to_pylist()

    def _search_predicate(
        self, business_domain: str, start_date: date | None, end_date: date | None
    ):
        builder = self._builder()
        predicates = [builder.equal("business_domain", business_domain)]
        predicates.extend(self._date_predicates(builder, start_date, end_date))
        return PredicateBuilder.and_predicates(predicates)

    def _partition_predicate(
        self,
        business_domain: str,
        department: str | None,
        start_date: date | None,
        end_date: date | None,
    ):
        raw = self._raw_table()
        builder = PredicateBuilder(raw.partition_keys_fields)
        predicates = [builder.equal("business_domain", business_domain)]
        if department:
            predicates.append(builder.equal("department", department))
        if start_date:
            predicates.append(
                builder.greater_or_equal("upload_month", start_date.strftime("%Y-%m"))
            )
        if end_date:
            predicates.append(builder.less_or_equal("upload_month", end_date.strftime("%Y-%m")))
        return PredicateBuilder.and_predicates(predicates)

    @staticmethod
    def _date_predicates(
        builder: PredicateBuilder, start_date: date | None, end_date: date | None
    ) -> Iterable:
        values = []
        if start_date:
            values.append(builder.greater_or_equal("created_at", start_date.isoformat()))
        if end_date:
            next_day = end_date + timedelta(days=1)
            values.append(builder.less_than("created_at", next_day.isoformat()))
        return values

    def _builder(self) -> PredicateBuilder:
        return PredicateBuilder(self._raw_table().fields)

    def _raw_table(self):
        return self._require_table().raw_table

    def _require_table(self):
        if self.table is None:
            raise StorageError("Paimon table is not initialized")
        return self.table
