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
    log_level: str = Field("INFO", alias="MORPHLAKE_LOG_LEVEL")
    api_key: str | None = Field(None, alias="MORPHLAKE_API_KEY")
    max_upload_mb: int = Field(200, alias="MORPHLAKE_MAX_UPLOAD_MB")
    models_config: Path = Field(Path("config/models.yaml"), alias="MORPHLAKE_MODELS_CONFIG")

    minio_endpoint: str = Field("localhost:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field("minioadmin", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field("minioadmin", alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(False, alias="MINIO_SECURE")
    minio_data_bucket: str = Field("morphlake-data", alias="MINIO_DATA_BUCKET")
    minio_paimon_bucket: str = Field("morphlake-paimon", alias="MINIO_PAIMON_BUCKET")
    minio_region: str | None = Field(None, alias="MINIO_REGION")

    paimon_database: str = Field("morphlake", alias="PAIMON_DATABASE")
    paimon_table: str = Field("multimodal_assets", alias="PAIMON_TABLE")
    paimon_warehouse: str = Field("s3://morphlake-paimon/warehouse", alias="PAIMON_WAREHOUSE")
    text_vector_dimension: int = Field(384, alias="PAIMON_TEXT_VECTOR_DIMENSION")
    image_vector_dimension: int = Field(512, alias="PAIMON_IMAGE_VECTOR_DIMENSION")
    audio_vector_dimension: int = Field(384, alias="PAIMON_AUDIO_VECTOR_DIMENSION")

    @field_validator("api_key", "minio_region", mode="before")
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
