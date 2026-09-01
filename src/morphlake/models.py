"""API contracts and internal records."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MediaType = Literal["document", "image", "audio"]


class Asset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_id: str
    filename: str
    media_type: MediaType
    content_type: str
    file_size: int
    business_domain: str
    department: str
    created_at: datetime
    object_bucket: str
    object_key: str
    object_etag: str | None = None
    chunk_count: int = 0


class AssetList(BaseModel):
    items: list[Asset]
    limit: int
    offset: int
    returned: int


class SearchHit(BaseModel):
    rank: int
    file_id: str
    filename: str
    media_type: MediaType
    business_domain: str
    department: str
    created_at: datetime
    record_type: Literal["file", "chunk"]
    chunk_index: int | None = None
    content_text: str | None = None


class SearchResult(BaseModel):
    items: list[SearchHit]
    returned: int


class FullTextSearchRequest(BaseModel):
    business_domain: str = Field(min_length=1, max_length=128)
    keyword: str = Field(min_length=1, max_length=1000)
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(20, ge=1, le=200)

    @model_validator(mode="after")
    def validate_range(self) -> FullTextSearchRequest:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class VectorSearchRequest(BaseModel):
    business_domain: str = Field(min_length=1, max_length=128)
    vector: list[float] = Field(min_length=1)
    vector_field: Literal["text", "image", "audio"] = "text"
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(10, ge=1, le=200)

    @field_validator("vector")
    @classmethod
    def finite_vector(cls, value: list[float]) -> list[float]:
        if any(item != item or item in (float("inf"), float("-inf")) for item in value):
            raise ValueError("vector must contain only finite values")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> VectorSearchRequest:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class ErrorBody(BaseModel):
    error: dict[str, Any]


class Health(BaseModel):
    status: Literal["ok", "not_ready"]
    checks: dict[str, str] | None = None
