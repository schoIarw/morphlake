"""Application service coordinating MinIO, models, and Paimon."""

from __future__ import annotations

import hashlib
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
from morphlake.partitioning import domain_shard
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
        self._index_error: str | None = None

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
            asset, text_segments, image_features, audio_features = self._records(
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
            self.catalog.add(
                asset=asset,
                text_segments=text_segments,
                image_features=image_features,
                audio_features=audio_features,
            )
            return asset
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
        department: str | None = None,
        start_date=None,
        end_date=None,
    ):
        """Vectorize an uploaded query file by modality and return Paimon Top 10."""
        filename = Path(filename).name.strip()
        business_domain = business_domain.strip()
        department = department.strip() if department else None
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
            department=department,
            vector=vector,
            vector_field=vector_field,
            start_date=start_date,
            end_date=end_date,
            limit=10,
        )

    def ready(self) -> dict[str, str]:
        checks: dict[str, str] = {}
        for name, callback in (
            ("minio", self.objects.ping),
            ("paimon", self.catalog.ping),
            ("models", self.models.ping),
        ):
            try:
                callback()
                checks[name] = "ok"
            except Exception as exc:  # readiness must report all failed dependencies
                checks[name] = f"error: {exc}"
        checks["indexes"] = f"error: {self._index_error}" if self._index_error else "ok"
        return checks

    def maintain(self) -> None:
        try:
            self.catalog.maintain_indexes()
            self._index_error = None
        except Exception as exc:
            self._index_error = str(exc)
            raise

    def flush_transfer_events(self, events: list[dict[str, Any]]) -> None:
        self.catalog.add_transfer_events(events)

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
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        chunks = []
        transcript = None
        image_vector = audio_vector = None
        if media_type == "document":
            text = extract_text(filename, body)
            chunks = chunk_text(text, self.models.chunk_size, self.models.chunk_overlap)
        elif media_type == "image":
            image_vector = self.models.embed_image(body, content_type)
        elif media_type == "audio":
            transcript = self.models.transcribe_audio(filename, body, content_type)
            audio_vector = self.models.embed_audio(body)

        shard = domain_shard(business_domain, self.settings.paimon_domain_shards)
        ingest_date = created_at.date().isoformat()
        common = {
            "file_id": file_id,
            "business_domain": business_domain,
            "department": department,
            "domain_shard": shard,
            "ingest_date": ingest_date,
            "created_at": created_at.isoformat(),
            "filename": filename,
            "media_type": media_type,
        }
        text_segments = []
        text_spec = self.models.specs["text_embedding"]
        for chunk in chunks:
            text_segments.append(
                {
                    **common,
                    "segment_id": f"{file_id}:{chunk.index}",
                    "record_type": "chunk",
                    "chunk_index": chunk.index,
                    "content_text": chunk.text,
                    "segment_type": "document_chunk",
                    "chunk_start": chunk.start,
                    "chunk_end": chunk.end,
                    "embedding_model": text_spec.model,
                    "embedding_version": "configured",
                    "text_embedding": self.models.embed_text(chunk.text),
                }
            )
        if transcript:
            text_segments.append(
                {
                    **common,
                    "segment_id": f"{file_id}:transcript:0",
                    "record_type": "chunk",
                    "chunk_index": 0,
                    "content_text": transcript,
                    "segment_type": "audio_transcript",
                    "chunk_start": 0,
                    "chunk_end": len(transcript),
                    "embedding_model": text_spec.model,
                    "embedding_version": "configured",
                    "text_embedding": self.models.embed_text(transcript),
                }
            )

        image_features = []
        if image_vector is not None:
            image_spec = self.models.specs["image_embedding"]
            image_features.append(
                {
                    **common,
                    "feature_id": f"{file_id}:image:0",
                    "record_type": "file",
                    "chunk_index": None,
                    "content_text": filename,
                    "feature_type": "whole_image",
                    "embedding_model": image_spec.model,
                    "embedding_version": "configured",
                    "image_embedding": image_vector,
                }
            )

        audio_features = []
        if audio_vector is not None:
            audio_spec = self.models.specs["audio_embedding"]
            audio_features.append(
                {
                    **common,
                    "feature_id": f"{file_id}:audio:0",
                    "record_type": "file",
                    "chunk_index": None,
                    "content_text": transcript,
                    "feature_type": "whole_audio",
                    "start_ms": None,
                    "end_ms": None,
                    "embedding_model": audio_spec.model,
                    "embedding_version": "configured",
                    "audio_embedding": audio_vector,
                }
            )

        asset = {
            **common,
            "content_type": content_type,
            "file_size": len(body),
            "content_sha256": hashlib.sha256(body).hexdigest(),
            "object_bucket": stored.bucket,
            "object_key": stored.key,
            "object_etag": stored.etag,
            "chunk_count": len(text_segments),
        }
        return asset, text_segments, image_features, audio_features

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
