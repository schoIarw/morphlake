"""Application service coordinating MinIO, models, and Paimon."""

from __future__ import annotations

import math
import mimetypes
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from morphlake.config import Settings
from morphlake.errors import ConfigurationError, MorphLakeError
from morphlake.models import FullTextSearchRequest, VectorSearchRequest
from morphlake.services.embeddings import ModelGateway
from morphlake.services.extractors import chunk_text, classify, extract_text
from morphlake.services.minio_store import MinioStore
from morphlake.services.paimon_store import PaimonStore


class MorphLakeService:
    def __init__(
        self,
        settings: Settings,
        objects: MinioStore | None = None,
        catalog: PaimonStore | None = None,
        models: ModelGateway | None = None,
    ):
        self.settings = settings
        self.objects = objects or MinioStore(settings)
        self.catalog = catalog or PaimonStore(settings)
        self.models = models or ModelGateway(settings.models_config)

    def initialize(self) -> None:
        self._validate_dimensions()
        self.objects.ensure_buckets()
        self.catalog.initialize()

    def upload(
        self,
        *,
        filename: str,
        content_type: str | None,
        body: bytes,
        business_domain: str,
        department: str,
        expected_media_type: str | None = None,
    ) -> dict[str, Any]:
        filename = Path(filename).name.strip()
        business_domain = business_domain.strip()
        department = department.strip()
        if not filename or not business_domain or not department:
            raise MorphLakeError(
                "invalid_upload", "filename, business_domain, and department are required"
            )
        if not body:
            raise MorphLakeError("empty_file", "Uploaded file is empty")
        if len(body) > self.settings.max_upload_bytes:
            raise MorphLakeError(
                "file_too_large",
                f"File exceeds the {self.settings.max_upload_mb} MiB upload limit",
                413,
            )
        media_type = classify(filename)
        if expected_media_type and media_type != expected_media_type:
            raise MorphLakeError(
                "media_type_mismatch",
                f"Expected a {expected_media_type} file, got {media_type}",
                415,
            )
        content_type = (
            content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        file_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        object_key = self._object_key(business_domain, department, created_at, file_id, filename)
        stored = self.objects.put(object_key, body, content_type)
        try:
            records = self._records(
                file_id=file_id,
                filename=filename,
                content_type=content_type,
                body=body,
                media_type=media_type,
                business_domain=business_domain,
                department=department,
                created_at=created_at,
                stored=stored,
            )
            self.catalog.add(records)
            return records[0]
        except Exception:
            self.objects.delete(object_key)
            raise

    def list_assets(self, **filters):
        return self.catalog.list_assets(**filters)

    def get_asset(self, file_id: str):
        return self.catalog.get_asset(file_id)

    def download(self, file_id: str):
        asset = self.catalog.get_asset(file_id)
        return asset, self.objects.stream(asset["object_key"])

    def full_text_search(self, request: FullTextSearchRequest):
        return self.catalog.full_text_search(**request.model_dump())

    def vector_search(self, request: VectorSearchRequest):
        return self.catalog.vector_search(**request.model_dump())

    def vector_search_file(
        self,
        *,
        filename: str,
        content_type: str | None,
        body: bytes,
        business_domain: str,
        start_date=None,
        end_date=None,
    ):
        """Vectorize an uploaded query file by modality and return Paimon Top 10."""
        filename = Path(filename).name.strip()
        business_domain = business_domain.strip()
        if not filename or not business_domain:
            raise MorphLakeError(
                "invalid_vector_query", "filename and business_domain are required"
            )
        if not body:
            raise MorphLakeError("empty_file", "Uploaded query file is empty")
        if len(body) > self.settings.max_upload_bytes:
            raise MorphLakeError(
                "file_too_large",
                f"File exceeds the {self.settings.max_upload_mb} MiB upload limit",
                413,
            )
        if start_date and end_date and start_date > end_date:
            raise MorphLakeError("invalid_date_range", "start_date must not be after end_date")

        media_type = classify(filename)
        resolved_content_type = (
            content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        if media_type == "document":
            text = extract_text(filename, body)
            chunks = chunk_text(text, self.models.chunk_size, self.models.chunk_overlap)
            if not chunks:
                raise MorphLakeError(
                    "document_text_empty",
                    "The query document contains no extractable text",
                    422,
                )
            vector = self._mean_vector([self.models.embed_text(chunk.text) for chunk in chunks])
            vector_field = "text"
        elif media_type == "image":
            vector = self.models.embed_image(body, resolved_content_type)
            vector_field = "image"
        else:
            vector = self.models.embed_audio(body)
            vector_field = "audio"

        return self.catalog.vector_search(
            business_domain=business_domain,
            vector=vector,
            vector_field=vector_field,
            start_date=start_date,
            end_date=end_date,
            limit=10,
        )

    def ready(self) -> dict[str, str]:
        checks: dict[str, str] = {}
        for name, callback in (("minio", self.objects.ping), ("paimon", self.catalog.ping)):
            try:
                callback()
                checks[name] = "ok"
            except Exception as exc:  # readiness must report all failed dependencies
                checks[name] = f"error: {exc}"
        return checks

    def _records(
        self,
        *,
        file_id: str,
        filename: str,
        content_type: str,
        body: bytes,
        media_type: str,
        business_domain: str,
        department: str,
        created_at: datetime,
        stored,
    ) -> list[dict[str, Any]]:
        chunks = []
        file_text = None
        text_vector = image_vector = audio_vector = None
        if media_type == "document":
            file_text = extract_text(filename, body)
            chunks = chunk_text(file_text, self.models.chunk_size, self.models.chunk_overlap)
        elif media_type == "image":
            file_text = filename
            image_vector = self.models.embed_image(body, content_type)
        elif media_type == "audio":
            file_text = self.models.transcribe_audio(filename, body, content_type)
            audio_vector = self.models.embed_audio(body)
            if file_text:
                text_vector = self.models.embed_text(file_text)

        base = {
            "file_id": file_id,
            "business_domain": business_domain,
            "department": department,
            "upload_month": created_at.strftime("%Y-%m"),
            "created_at": created_at.isoformat(),
            "filename": filename,
            "media_type": media_type,
            "content_type": content_type,
            "file_size": len(body),
            "object_bucket": stored.bucket,
            "object_key": stored.key,
            "object_etag": stored.etag,
            "chunk_count": len(chunks),
        }
        records = [
            {
                **base,
                "row_id": file_id,
                "record_type": "file",
                "chunk_index": None,
                "chunk_start": None,
                "chunk_end": None,
                "content_text": file_text if media_type != "document" else None,
                "text_embedding": text_vector,
                "image_embedding": image_vector,
                "audio_embedding": audio_vector,
            }
        ]
        for chunk in chunks:
            records.append(
                {
                    **base,
                    "row_id": f"{file_id}:{chunk.index}",
                    "record_type": "chunk",
                    "chunk_index": chunk.index,
                    "chunk_start": chunk.start,
                    "chunk_end": chunk.end,
                    "content_text": chunk.text,
                    "text_embedding": self.models.embed_text(chunk.text),
                    "image_embedding": None,
                    "audio_embedding": None,
                }
            )
        return records

    def _validate_dimensions(self) -> None:
        mapping = {
            "text_embedding": self.settings.text_vector_dimension,
            "image_embedding": self.settings.image_vector_dimension,
            "audio_embedding": self.settings.audio_vector_dimension,
        }
        for name, expected in mapping.items():
            configured = self.models.specs[name].dimension
            if configured != expected:
                raise ConfigurationError(
                    f"{name} dimension is {configured}; Paimon expects {expected}"
                )

    @staticmethod
    def _mean_vector(vectors: list[list[float]]) -> list[float]:
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise ConfigurationError("Document chunk embeddings have inconsistent dimensions")
        mean = [sum(values) / len(vectors) for values in zip(*vectors, strict=True)]
        norm = math.sqrt(sum(value * value for value in mean)) or 1.0
        return [value / norm for value in mean]

    @staticmethod
    def _object_key(
        business_domain: str,
        department: str,
        created_at: datetime,
        file_id: str,
        filename: str,
    ) -> str:
        def safe(value: str) -> str:
            return re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")

        return "/".join(
            [
                safe(business_domain) or "domain",
                safe(department) or "department",
                created_at.strftime("%Y/%m/%d"),
                file_id,
                safe(filename) or "file",
            ]
        )
