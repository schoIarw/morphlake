"""Independent MorphLake administration ASGI service."""

from __future__ import annotations

import logging
import secrets
import time

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from morphlake import __version__
from morphlake.admin import router
from morphlake.admin_store import AdminStore
from morphlake.auth import get_admin_store
from morphlake.config import Settings, get_settings
from morphlake.errors import MorphLakeError
from morphlake.metrics import Metrics

LOGGER = logging.getLogger(__name__)


def create_admin_app(
    settings: Settings | None = None,
    store: AdminStore | None = None,
    metrics: Metrics | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or AdminStore(settings)
    metrics = metrics or Metrics()
    store.initialize()

    application = FastAPI(
        title="MorphLake Administration",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_admin_store] = lambda: store
    application.state.admin_store = store
    application.state.metrics = metrics

    @application.middleware("http")
    async def observe_http(request: Request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_name = getattr(route, "path", None) or "unmatched"
            metrics.http_requests.labels(request.method, route_name, str(status)).inc()
            metrics.http_duration.labels(request.method, route_name).observe(
                time.monotonic() - started
            )

    @application.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        store.list_tokens()
        return {"status": "ok"}

    @application.get("/metrics", include_in_schema=False)
    def prometheus_metrics(
        authorization: str | None = Header(None),
        x_metrics_token: str | None = Header(None),
    ) -> Response:
        _check_metrics_token(settings, authorization, x_metrics_token)
        metrics.component_up.labels("sqlite").set(1)
        return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.exception_handler(MorphLakeError)
    async def handle_morphlake_error(_: Request, exc: MorphLakeError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=exc.headers,
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

    return application


def _check_metrics_token(
    settings: Settings, authorization: str | None, x_metrics_token: str | None
) -> None:
    if not settings.metrics_token:
        raise MorphLakeError(
            "metrics_not_configured", "MORPHLAKE_METRICS_TOKEN is not configured", 503
        )
    presented = x_metrics_token
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = credentials
    if not presented or not secrets.compare_digest(presented, settings.metrics_token):
        raise MorphLakeError("metrics_token_invalid", "Metrics token is invalid", 401)


app = create_admin_app()


def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        "morphlake.admin_main:app",
        host=settings.host,
        port=settings.admin_port,
        workers=1,
    )


if __name__ == "__main__":
    run()
