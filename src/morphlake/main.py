"""MorphLake ASGI entry point."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response

from morphlake import __version__
from morphlake.admin import require_admin
from morphlake.admin_store import AdminStore
from morphlake.api import get_service, router
from morphlake.auth import get_admin_store
from morphlake.config import Settings, get_settings
from morphlake.errors import MorphLakeError
from morphlake.metrics import Metrics, get_metrics
from morphlake.services.morphlake import MorphLakeService

LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    service: MorphLakeService | None = None,
    admin_store: AdminStore | None = None,
    metrics: Metrics | None = None,
    *,
    initialize: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    service = service or MorphLakeService(settings)
    admin_store = admin_store or AdminStore(settings)
    metrics = metrics or Metrics()
    admin_store.initialize()
    metrics.management_db_info.labels(admin_store.backend).set(1)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks: list[asyncio.Task] = []
        if initialize:
            await asyncio.to_thread(service.initialize)
            tasks.extend(
                [
                    asyncio.create_task(
                        _index_maintenance_loop(
                            service,
                            metrics,
                            settings.paimon_index_build_interval_seconds,
                        )
                    ),
                    asyncio.create_task(_audit_flush_loop(service, admin_store, metrics, settings)),
                    asyncio.create_task(
                        _health_monitor_loop(
                            service,
                            admin_store,
                            metrics,
                            settings.health_check_interval_seconds,
                        )
                    ),
                ]
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if initialize:
                await _flush_audit_once(service, admin_store, metrics, settings)

    application = FastAPI(
        title="MorphLake API",
        version=__version__,
        description="Paimon 2.0 multimodal foundation using descriptor-only objects in MinIO",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(router)
    application.dependency_overrides[get_service] = lambda: service
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_admin_store] = lambda: admin_store
    application.dependency_overrides[get_metrics] = lambda: metrics
    application.state.service = service
    application.state.admin_store = admin_store
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

    @application.get("/metrics", include_in_schema=False)
    def prometheus_metrics(
        authorization: str | None = Header(None),
        x_metrics_token: str | None = Header(None),
    ) -> Response:
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
        return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.get("/openapi.json", include_in_schema=False)
    def openapi_schema(_: str = Depends(require_admin)):
        return JSONResponse(application.openapi())

    @application.get("/docs", include_in_schema=False)
    def swagger_ui(_: str = Depends(require_admin)) -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="MorphLake API")

    @application.exception_handler(MorphLakeError)
    async def handle_morphlake_error(_: Request, exc: MorphLakeError):
        if exc.code.startswith("token_") or exc.code.startswith("metrics_token"):
            metrics.auth_failures.labels(exc.code).inc()
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


async def _index_maintenance_loop(
    service: MorphLakeService, metrics: Metrics, interval_seconds: int
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        started = time.monotonic()
        try:
            await asyncio.to_thread(service.maintain)
            metrics.index_runs.labels("success").inc()
            metrics.index_last_success.set(time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.index_runs.labels("failed").inc()
            LOGGER.exception("Scheduled Paimon index maintenance failed")
        finally:
            metrics.index_duration.observe(time.monotonic() - started)


async def _audit_flush_loop(
    service: MorphLakeService,
    store: AdminStore,
    metrics: Metrics,
    settings: Settings,
) -> None:
    while True:
        await asyncio.sleep(settings.paimon_audit_flush_interval_seconds)
        await _flush_audit_once(service, store, metrics, settings)


async def _flush_audit_once(
    service: MorphLakeService,
    store: AdminStore,
    metrics: Metrics,
    settings: Settings,
) -> None:
    claim_id, events = store.claim_transfer_events(settings.paimon_audit_batch_size)
    metrics.audit_backlog.set(store.unsynced_event_count())
    if not events:
        return
    try:
        await asyncio.to_thread(service.flush_transfer_events, events)
        store.mark_events_synced([event["event_id"] for event in events])
        store.cleanup_synced_events()
        metrics.audit_flush.labels("success").inc()
    except asyncio.CancelledError:
        store.release_event_claim(claim_id)
        raise
    except Exception:
        store.release_event_claim(claim_id)
        metrics.audit_flush.labels("failed").inc()
        LOGGER.exception("Paimon transfer audit flush failed")
    finally:
        metrics.audit_backlog.set(store.unsynced_event_count())


async def _health_monitor_loop(
    service: MorphLakeService,
    store: AdminStore,
    metrics: Metrics,
    interval_seconds: int,
) -> None:
    while True:
        checks = await asyncio.to_thread(service.ready)
        for component, status in checks.items():
            metrics.component_up.labels(component).set(1 if status == "ok" else 0)
        try:
            await asyncio.to_thread(store.ping)
            metrics.component_up.labels("management_db").set(1)
        except Exception:
            metrics.component_up.labels("management_db").set(0)
            LOGGER.exception("Management database health check failed")
        await asyncio.sleep(interval_seconds)


app = create_app()


def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run("morphlake.main:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    run()
