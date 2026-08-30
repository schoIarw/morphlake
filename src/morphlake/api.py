"""Versioned FastAPI contract."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import StreamingResponse

from morphlake.config import Settings, get_settings
from morphlake.errors import MorphLakeError
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


def require_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,
) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise MorphLakeError("unauthorized", "A valid X-API-Key header is required", 401)


@router.get("/health/live", response_model=Health, tags=["health"])
def live() -> Health:
    return Health(status="ok")


@router.get("/health/ready", response_model=Health, tags=["health"])
def ready(
    response: Response,
    service: Annotated[MorphLakeService, Depends(get_service)],
) -> Health:
    checks = service.ready()
    is_ready = all(value == "ok" for value in checks.values())
    if not is_ready:
        response.status_code = 503
    return Health(status="ok" if is_ready else "not_ready", checks=checks)


@router.post(
    "/api/v1/files",
    response_model=Asset,
    status_code=201,
    dependencies=[Depends(require_api_key)],
    tags=["files"],
)
async def upload_file(
    file: Annotated[UploadFile, File()],
    business_domain: Annotated[str, Form(min_length=1, max_length=128)],
    department: Annotated[str, Form(min_length=1, max_length=128)],
    service: Annotated[MorphLakeService, Depends(get_service)],
) -> Asset:
    body = await file.read()
    result = service.upload(
        filename=file.filename or "",
        content_type=file.content_type,
        body=body,
        business_domain=business_domain,
        department=department,
    )
    return Asset.model_validate(result)


@router.get(
    "/api/v1/files",
    response_model=AssetList,
    dependencies=[Depends(require_api_key)],
    tags=["files"],
)
def list_files(
    service: Annotated[MorphLakeService, Depends(get_service)],
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


@router.get(
    "/api/v1/files/{file_id}/download",
    dependencies=[Depends(require_api_key)],
    tags=["files"],
)
def download_file(file_id: str, service: Annotated[MorphLakeService, Depends(get_service)]):
    asset, stream = service.download(file_id)
    encoded = quote(asset["filename"])
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "Content-Length": str(asset["file_size"]),
        "ETag": asset.get("object_etag") or "",
    }
    return StreamingResponse(stream, media_type=asset["content_type"], headers=headers)


@router.post(
    "/api/v1/search/full-text",
    response_model=SearchResult,
    dependencies=[Depends(require_api_key)],
    tags=["search"],
)
def full_text_search(
    request: FullTextSearchRequest,
    service: Annotated[MorphLakeService, Depends(get_service)],
) -> SearchResult:
    rows = service.full_text_search(request)
    items = [SearchHit(rank=index + 1, **row) for index, row in enumerate(rows)]
    return SearchResult(items=items, returned=len(items))


@router.post(
    "/api/v1/search/vector",
    response_model=SearchResult,
    dependencies=[Depends(require_api_key)],
    tags=["search"],
)
def vector_search(
    request: VectorSearchRequest,
    service: Annotated[MorphLakeService, Depends(get_service)],
) -> SearchResult:
    rows = service.vector_search(request)
    items = [SearchHit(rank=index + 1, **row) for index, row in enumerate(rows)]
    return SearchResult(items=items, returned=len(items))
