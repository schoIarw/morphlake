"""MorphLake ASGI entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from morphlake import __version__
from morphlake.api import get_service, router
from morphlake.config import Settings, get_settings
from morphlake.errors import MorphLakeError
from morphlake.services.morphlake import MorphLakeService

LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    service: MorphLakeService | None = None,
    *,
    initialize: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    service = service or MorphLakeService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if initialize:
            service.initialize()
        yield

    application = FastAPI(
        title="MorphLake API",
        version=__version__,
        description="Paimon 2.0 multimodal foundation using descriptor-only objects in MinIO",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.dependency_overrides[get_service] = lambda: service
    application.dependency_overrides[get_settings] = lambda: settings

    @application.exception_handler(MorphLakeError)
    async def handle_morphlake_error(_: Request, exc: MorphLakeError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed",
                    "details": exc.errors(),
                }
            },
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception):
        LOGGER.exception("Unhandled request error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The request could not be completed",
                }
            },
        )

    return application


app = create_app()


def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        "morphlake.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
    )


if __name__ == "__main__":
    run()
