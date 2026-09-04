"""Environment and model configuration."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?}")


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = Field("development", alias="MORPHLAKE_ENV")
    host: str = Field("0.0.0.0", alias="MORPHLAKE_HOST")
    port: int = Field(8080, alias="MORPHLAKE_PORT")
    admin_port: int = Field(8081, alias="MORPHLAKE_ADMIN_PORT")
    log_level: str = Field("INFO", alias="MORPHLAKE_LOG_LEVEL")
    max_upload_mb: int = Field(200, alias="MORPHLAKE_MAX_UPLOAD_MB")
    models_config: Path = Field(Path("config/models.yaml"), alias="MORPHLAKE_MODELS_CONFIG")
    admin_db_path: Path = Field(Path("data/morphlake-admin.db"), alias="MORPHLAKE_ADMIN_DB_PATH")
    admin_username: str = Field("admin", alias="MORPHLAKE_ADMIN_USERNAME")
    admin_password: str = Field("change-me", alias="MORPHLAKE_ADMIN_PASSWORD")
    token_pepper: str = Field("change-me-in-production", alias="MORPHLAKE_TOKEN_PEPPER")
    metrics_token: str | None = Field(None, alias="MORPHLAKE_METRICS_TOKEN")
    prometheus_url: str | None = Field(None, alias="PROMETHEUS_URL")
    health_check_interval_seconds: int = Field(
        15, alias="MORPHLAKE_HEALTH_CHECK_INTERVAL_SECONDS", ge=5
    )
    default_rate_period_seconds: int = Field(
        60, alias="MORPHLAKE_DEFAULT_RATE_PERIOD_SECONDS", ge=1
    )
    default_upload_requests: int = Field(60, alias="MORPHLAKE_DEFAULT_UPLOAD_REQUESTS", ge=0)
    default_download_requests: int = Field(120, alias="MORPHLAKE_DEFAULT_DOWNLOAD_REQUESTS", ge=0)
    default_upload_bytes: int = Field(1_073_741_824, alias="MORPHLAKE_DEFAULT_UPLOAD_BYTES", ge=0)
    default_download_bytes: int = Field(
        5_368_709_120, alias="MORPHLAKE_DEFAULT_DOWNLOAD_BYTES", ge=0
    )

    minio_endpoint: str = Field("localhost:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field("minioadmin", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field("minioadmin", alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(False, alias="MINIO_SECURE")
    minio_data_bucket: str = Field("morphlake-data", alias="MINIO_DATA_BUCKET")
    minio_paimon_bucket: str = Field("morphlake-paimon", alias="MINIO_PAIMON_BUCKET")
    minio_region: str | None = Field(None, alias="MINIO_REGION")

    paimon_database: str = Field("morphlake", alias="PAIMON_DATABASE")
    paimon_table: str = Field("multimodal_asset_descriptor", alias="PAIMON_TABLE")
    paimon_text_table: str = Field("multimodal_text_segment", alias="PAIMON_TEXT_TABLE")
    paimon_image_table: str = Field("multimodal_image_feature", alias="PAIMON_IMAGE_TABLE")
    paimon_audio_table: str = Field("multimodal_audio_feature", alias="PAIMON_AUDIO_TABLE")
    paimon_audit_table: str = Field("multimodal_transfer_audit", alias="PAIMON_AUDIT_TABLE")
    paimon_warehouse: str = Field("s3://morphlake-paimon/warehouse", alias="PAIMON_WAREHOUSE")
    paimon_domain_shards: int = Field(32, alias="PAIMON_DOMAIN_SHARDS", ge=1, le=4096)
    paimon_vector_index_type: str = Field("ivf-sq", alias="PAIMON_VECTOR_INDEX_TYPE")
    paimon_index_build_interval_seconds: int = Field(
        300, alias="PAIMON_INDEX_BUILD_INTERVAL_SECONDS", ge=1
    )
    paimon_index_row_count_per_shard: int = Field(
        500_000, alias="PAIMON_INDEX_ROW_COUNT_PER_SHARD", ge=1
    )
    paimon_audit_flush_interval_seconds: int = Field(
        5, alias="PAIMON_AUDIT_FLUSH_INTERVAL_SECONDS", ge=1
    )
    paimon_audit_batch_size: int = Field(1_000, alias="PAIMON_AUDIT_BATCH_SIZE", ge=1, le=100_000)
    transfer_sqlite_retention_days: int = Field(
        30, alias="MORPHLAKE_TRANSFER_SQLITE_RETENTION_DAYS", ge=1
    )
    text_vector_dimension: int = Field(384, alias="PAIMON_TEXT_VECTOR_DIMENSION")
    image_vector_dimension: int = Field(512, alias="PAIMON_IMAGE_VECTOR_DIMENSION")
    audio_vector_dimension: int = Field(384, alias="PAIMON_AUDIO_VECTOR_DIMENSION")

    @field_validator("paimon_vector_index_type")
    @classmethod
    def supported_vector_index(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"ivf-flat", "ivf-pq", "ivf-sq", "ivf-rq", "diskann"}:
            raise ValueError("PAIMON_VECTOR_INDEX_TYPE is not supported by PyPaimon 2.0")
        return value

    @field_validator("minio_region", "metrics_token", "prometheus_url", mode="before")
    @classmethod
    def empty_to_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def paimon_catalog_options(self) -> dict[str, str]:
        scheme = "https" if self.minio_secure else "http"
        return {
            "warehouse": self.paimon_warehouse,
            "s3.endpoint": f"{scheme}://{self.minio_endpoint}",
            "s3.access-key": self.minio_access_key,
            "s3.secret-key": self.minio_secret_key,
            "s3.path-style-access": "true",
        }


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.getenv(name, default or "")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def load_model_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Model configuration does not exist: {path}")
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream) or {}
    if "models" not in value or "chunking" not in value:
        raise ValueError("Model configuration must contain 'models' and 'chunking'")
    return _expand_env(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
