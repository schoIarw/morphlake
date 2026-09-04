"""Versioned FastAPI contract with scoped token authentication."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from morphlake.admin_store import AdminStore, TokenIdentity
from morphlake.auth import enforce_scope, get_admin_store, require_token
from morphlake.errors import MorphLakeError
from morphlake.metrics import Metrics, get_metrics
from morphlake.models import (
    Asset,
    AssetList,
    FullTextSearchRequest,
    Health,
    SearchHit,
    SearchResult,
    VectorSearchRequest,
)
from morphlake.services.morphlake import MorphLakeService

router = APIRouter()


def get_service() -> MorphLakeService:
    raise RuntimeError("Application service dependency is not configured")


@router.get("/health/live", response_model=Health, tags=["health"])
def live() -> Health:
    """Public process liveness endpoint used by the container runtime."""
    return Health(status="ok")


@router.get("/health/ready", response_model=Health, tags=["health"])
def ready(
    response: Response,
    service: Annotated[MorphLakeService, Depends(get_service)],
    _: Annotated[TokenIdentity, Depends(require_token)],
) -> Health:
    checks = service.ready()
    is_ready = all(value == "ok" for value in checks.values())
    if not is_ready:
        response.status_code = 503
    return Health(status="ok" if is_ready else "not_ready", checks=checks)


@router.post("/api/v1/files", response_model=Asset, status_code=201, tags=["files"])
async def upload_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    business_domain: Annotated[str, Form(min_length=1, max_length=128)],
    department: Annotated[str, Form(min_length=1, max_length=128)],
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
) -> Asset:
    return await _upload_typed_file(
        expected_media_type=None,
        request=request,
        file=file,
        business_domain=business_domain,
        department=department,
        service=service,
        identity=identity,
        store=store,
        metrics=metrics,
    )


async def _upload_typed_file(
    *,
    expected_media_type: str | None,
    request: Request,
    file: UploadFile,
    business_domain: str,
    department: str,
    service: MorphLakeService,
    identity: TokenIdentity,
    store: AdminStore,
    metrics: Metrics,
) -> Asset:
    business_domain, department = enforce_scope(identity, business_domain, department)
    body = await file.read()
    filename = file.filename or ""
    started = time.monotonic()
    _consume_rate(
        store=store,
        metrics=metrics,
        identity=identity,
        operation="upload",
        byte_count=len(body),
        request=request,
        filename=filename,
        media_type=expected_media_type,
    )
    try:
        result = await asyncio.to_thread(
            service.upload,
            filename=filename,
            content_type=file.content_type,
            body=body,
            business_domain=business_domain,
            department=department,
            expected_media_type=expected_media_type,
        )
    except Exception as exc:
        _record_transfer(
            store,
            metrics,
            identity,
            request,
            operation="upload",
            filename=filename,
            byte_count=len(body),
            duration_ms=_duration_ms(started),
            status="failed",
            media_type=expected_media_type,
            error_code=_error_code(exc),
        )
        raise
    _record_transfer(
        store,
        metrics,
        identity,
        request,
        operation="upload",
        filename=filename,
        byte_count=len(body),
        duration_ms=_duration_ms(started),
        status="success",
        file_id=result["file_id"],
        media_type=result["media_type"],
    )
    return Asset.model_validate(result)


@router.post("/api/v1/files/documents", response_model=Asset, status_code=201, tags=["files"])
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    business_domain: Annotated[str, Form(min_length=1, max_length=128)],
    department: Annotated[str, Form(min_length=1, max_length=128)],
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
) -> Asset:
    return await _upload_typed_file(
        expected_media_type="document",
        request=request,
        file=file,
        business_domain=business_domain,
        department=department,
        service=service,
        identity=identity,
        store=store,
        metrics=metrics,
    )


@router.post("/api/v1/files/images", response_model=Asset, status_code=201, tags=["files"])
async def upload_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    business_domain: Annotated[str, Form(min_length=1, max_length=128)],
    department: Annotated[str, Form(min_length=1, max_length=128)],
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
) -> Asset:
    return await _upload_typed_file(
        expected_media_type="image",
        request=request,
        file=file,
        business_domain=business_domain,
        department=department,
        service=service,
        identity=identity,
        store=store,
        metrics=metrics,
    )


@router.post("/api/v1/files/audio", response_model=Asset, status_code=201, tags=["files"])
async def upload_audio(
    request: Request,
    file: Annotated[UploadFile, File()],
    business_domain: Annotated[str, Form(min_length=1, max_length=128)],
    department: Annotated[str, Form(min_length=1, max_length=128)],
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
) -> Asset:
    return await _upload_typed_file(
        expected_media_type="audio",
        request=request,
        file=file,
        business_domain=business_domain,
        department=department,
        service=service,
        identity=identity,
        store=store,
        metrics=metrics,
    )


@router.get("/api/v1/files", response_model=AssetList, tags=["files"])
def list_files(
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    media_type: Annotated[str | None, Query(pattern="^(document|image|audio)$")] = None,
    business_domain: Annotated[str | None, Query(max_length=128)] = None,
    department: Annotated[str | None, Query(max_length=128)] = None,
    filename: Annotated[str | None, Query(max_length=255)] = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AssetList:
    if start_date and end_date and start_date > end_date:
        raise MorphLakeError("invalid_date_range", "start_date must not be after end_date")
    business_domain, department = enforce_scope(identity, business_domain, department)
    rows = service.list_assets(
        media_type=media_type,
        business_domain=business_domain,
        department=department,
        filename=filename,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    items = [Asset.model_validate(row) for row in rows]
    return AssetList(items=items, limit=limit, offset=offset, returned=len(items))


@router.get("/api/v1/files/{file_id}/download", tags=["files"])
def download_file(
    file_id: str,
    request: Request,
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
):
    started = time.monotonic()
    asset, stream = service.download(file_id)
    enforce_scope(identity, asset["business_domain"], asset["department"])
    _consume_rate(
        store=store,
        metrics=metrics,
        identity=identity,
        operation="download",
        byte_count=asset["file_size"],
        request=request,
        filename=asset["filename"],
        media_type=asset["media_type"],
        file_id=file_id,
    )

    def audited_stream() -> Iterator[bytes]:
        transferred = 0
        status = "success"
        error_code = None
        try:
            for chunk in stream:
                transferred += len(chunk)
                yield chunk
        except Exception as exc:
            status = "failed"
            error_code = _error_code(exc)
            raise
        finally:
            _record_transfer(
                store,
                metrics,
                identity,
                request,
                operation="download",
                filename=asset["filename"],
                byte_count=transferred,
                duration_ms=_duration_ms(started),
                status=status,
                file_id=file_id,
                media_type=asset["media_type"],
                error_code=error_code,
            )

    encoded = quote(asset["filename"])
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "Content-Length": str(asset["file_size"]),
        "ETag": asset.get("object_etag") or "",
    }
    return StreamingResponse(audited_stream(), media_type=asset["content_type"], headers=headers)


@router.post("/api/v1/search/full-text", response_model=SearchResult, tags=["search"])
def full_text_search(
    request: FullTextSearchRequest,
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
) -> SearchResult:
    domain, department = enforce_scope(identity, request.business_domain, request.department)
    request = request.model_copy(update={"business_domain": domain, "department": department})
    rows = service.full_text_search(request)
    items = [SearchHit(rank=index + 1, **row) for index, row in enumerate(rows)]
    return SearchResult(items=items, returned=len(items))


@router.post("/api/v1/search/vector", response_model=SearchResult, tags=["search"])
def vector_search(
    request: VectorSearchRequest,
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
) -> SearchResult:
    domain, department = enforce_scope(identity, request.business_domain, request.department)
    request = request.model_copy(update={"business_domain": domain, "department": department})
    rows = service.vector_search(request)
    items = [SearchHit(rank=index + 1, **row) for index, row in enumerate(rows)]
    return SearchResult(items=items, returned=len(items))


@router.post("/api/v1/search/vector/file", response_model=SearchResult, tags=["search"])
async def vector_search_file(
    file: Annotated[UploadFile, File()],
    business_domain: Annotated[str, Form(min_length=1, max_length=128)],
    service: Annotated[MorphLakeService, Depends(get_service)],
    identity: Annotated[TokenIdentity, Depends(require_token)],
    department: Annotated[str | None, Form(min_length=1, max_length=128)] = None,
    start_date: Annotated[date | None, Form()] = None,
    end_date: Annotated[date | None, Form()] = None,
) -> SearchResult:
    business_domain, department = enforce_scope(identity, business_domain, department)
    body = await file.read()
    rows = await asyncio.to_thread(
        service.vector_search_file,
        filename=file.filename or "",
        content_type=file.content_type,
        body=body,
        business_domain=business_domain,
        department=department,
        start_date=start_date,
        end_date=end_date,
    )
    items = [SearchHit(rank=index + 1, **row) for index, row in enumerate(rows)]
    return SearchResult(items=items, returned=len(items))


def _consume_rate(
    *,
    store: AdminStore,
    metrics: Metrics,
    identity: TokenIdentity,
    operation: str,
    byte_count: int,
    request: Request,
    filename: str,
    media_type: str | None,
    file_id: str | None = None,
) -> None:
    try:
        store.consume_rate(identity, operation, byte_count)
    except MorphLakeError as exc:
        metrics.rate_limited.labels(operation, identity.business_domain, identity.department).inc()
        _record_transfer(
            store,
            metrics,
            identity,
            request,
            operation=operation,
            filename=filename,
            byte_count=0,
            duration_ms=0,
            status="rejected",
            file_id=file_id,
            media_type=media_type,
            error_code=exc.code,
        )
        raise


def _record_transfer(
    store: AdminStore,
    metrics: Metrics,
    identity: TokenIdentity,
    request: Request,
    *,
    operation: str,
    filename: str,
    byte_count: int,
    duration_ms: int,
    status: str,
    file_id: str | None = None,
    media_type: str | None = None,
    error_code: str | None = None,
) -> None:
    client_ip = request.client.host if request.client else None
    store.record_transfer(
        identity=identity,
        operation=operation,
        filename=filename,
        byte_count=byte_count,
        duration_ms=duration_ms,
        status=status,
        file_id=file_id,
        media_type=media_type,
        error_code=error_code,
        client_ip=client_ip,
        user_agent=request.headers.get("user-agent"),
    )
    metrics.observe_transfer(
        operation=operation,
        business_domain=identity.business_domain,
        department=identity.department,
        status=status,
        byte_count=byte_count,
    )


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, MorphLakeError) else type(exc).__name__
