from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import select, text

from .api import admin, announcements, auth, captcha_learning, sync, websocket
from .bootstrap import bootstrap_database
from .config import get_settings
from .database import Base, SessionLocal, engine
from .errors import error_body, install_error_handlers
from .models import Account
from .update_manifest import router as update_manifest_router

REQUEST_COUNT = Counter(
    "intdemo_http_requests_total",
    "Total HTTP requests",
    ("method", "path", "status"),
)
REQUEST_LATENCY = Histogram(
    "intdemo_http_request_duration_seconds",
    "HTTP request duration",
    ("method", "path"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.is_production:
        Base.metadata.create_all(engine)
    if settings.bootstrap_enabled:
        with SessionLocal() as db:
            bootstrap_database(db)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="IntDemo Sync API",
        version="1.1.0",
        docs_url="/api/v1/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        if (
            settings.maintenance_mode
            and request.url.path
            not in {
                "/api/v1/health/live",
                "/api/v1/health/ready",
                "/api/v1/metrics",
            }
            and not request.url.path.startswith("/updates/")
        ):
            return JSONResponse(
                status_code=503,
                content=error_body(
                    request,
                    "maintenance_mode",
                    "服务器正在维护，客户端数据会保留在待上传队列中",
                    True,
                ),
                headers={
                    "Retry-After": "60",
                    "X-Request-ID": request.state.request_id,
                    "X-Content-Type-Options": "nosniff",
                },
            )
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "__unmatched__")
        REQUEST_COUNT.labels(request.method, path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(time.perf_counter() - started)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    api = APIRouter(prefix="/api/v1")
    api.include_router(auth.router)
    api.include_router(admin.router)
    api.include_router(announcements.router)
    api.include_router(captcha_learning.router)
    api.include_router(sync.router)
    api.include_router(websocket.router)

    @api.get("/health/live")
    def live() -> dict:
        return {"status": "live", "version": "1.1.0"}

    @api.get("/health/ready")
    def ready() -> dict:
        if settings.maintenance_mode:
            return Response(
                content='{"status":"not_ready","reason":"maintenance_mode"}',
                status_code=503,
                media_type="application/json",
            )
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            admin_account = db.scalar(select(Account).where(Account.username == "admin"))
            if not admin_account or admin_account.must_change_password:
                return Response(
                    content='{"status":"not_ready","reason":"admin_password_change_required"}',
                    status_code=503,
                    media_type="application/json",
                )
        return {"status": "ready"}

    @api.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(api)
    app.include_router(update_manifest_router)
    return app


app = create_app()
