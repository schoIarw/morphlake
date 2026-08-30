"""MinIO object storage adapter."""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass

from minio import Minio
from minio.error import S3Error

from morphlake.config import Settings
from morphlake.errors import NotFoundError, StorageError


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    etag: str
    size: int


class MinioStore:
    def __init__(self, settings: Settings):
        self.bucket = settings.minio_data_bucket
        self.paimon_bucket = settings.minio_paimon_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            region=settings.minio_region,
        )

    def ensure_buckets(self) -> None:
        for bucket in (self.bucket, self.paimon_bucket):
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

    def put(self, key: str, body: bytes, content_type: str) -> StoredObject:
        try:
            result = self.client.put_object(
                self.bucket,
                key,
                io.BytesIO(body),
                len(body),
                content_type=content_type,
            )
            return StoredObject(self.bucket, key, result.etag, len(body))
        except S3Error as exc:
            raise StorageError(f"MinIO upload failed: {exc.code}") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.remove_object(self.bucket, key)
        except S3Error as exc:
            raise StorageError(f"MinIO cleanup failed: {exc.code}") from exc

    def stream(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            response = self.client.get_object(self.bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                raise NotFoundError("Object does not exist") from exc
            raise StorageError(f"MinIO download failed: {exc.code}") from exc
        try:
            while data := response.read(chunk_size):
                yield data
        finally:
            response.close()
            response.release_conn()

    def ping(self) -> None:
        self.client.bucket_exists(self.bucket)
