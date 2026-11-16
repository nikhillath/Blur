import asyncio
import logging
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1 import admin, auth, dashboard, documents, health, redaction, review, scans
from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.document_processing.ocr import ocr_available
from app.ml.transformer import get_transformer_detector
from app.services.scan_runner import recover_stale_scans

logger = logging.getLogger("pii_sentinel")


async def _retention_loop(interval_hours: int) -> None:
    """Background coroutine: runs the retention purge every RETENTION_INTERVAL_HOURS."""
    from app.db.session import SessionLocal
    from app.services.retention import purge_expired

    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            with SessionLocal() as db:
                counts = purge_expired(db)
            logger.info("retention purge: %s", counts)
        except Exception:
            logger.exception("retention purge failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    try:
        recovered = recover_stale_scans(startup=True)
        if recovered:
            logger.warning("marked %d interrupted scan(s) as FAILED", recovered)
    except Exception:
        logger.exception("could not recover stale scans (is the database reachable?)")
    if s.ocr_warmup:  # load OCR models off the request path so the first scan is not slow
        threading.Thread(target=ocr_available, name="ocr-warmup", daemon=True).start()
        threading.Thread(target=get_transformer_detector, name="ner-warmup", daemon=True).start()
    # Start retention loop only when at least one retention window is enabled.
    if s.retention_days > 0 or s.audit_retention_days > 0:
        task = asyncio.create_task(_retention_loop(s.retention_interval_hours))
        logger.info("retention loop started (interval %dh)", s.retention_interval_hours)
    else:
        task = None
    yield
    if task is not None:
        task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    dev = settings.environment == "development"
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan,
                  docs_url="/docs" if dev else None, redoc_url=None, openapi_url="/openapi.json" if dev else None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not dev:  # the interactive docs need inline scripts, so only lock this down outside development
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.exception_handler(DataError)
    async def bad_data(request: Request, exc: DataError) -> JSONResponse:
        # e.g. a NUL byte in a text field, which PostgreSQL cannot store: a client error, not a server fault
        logger.warning("rejected input the database cannot store on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=422, content={"detail": "input contains characters that cannot be stored"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI echoes the submitted value in each error ("input"); never send a rejected password back.
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_upload_bytes + 1024 * 1024:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
        if request.method in ("POST", "PATCH", "PUT") and length is None and "chunked" in request.headers.get("transfer-encoding", "").lower():
            return JSONResponse(status_code=411, content={"detail": "Content-Length required"})  # unbounded streamed bodies
        return await call_next(request)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(scans.router, prefix="/api/v1")
    app.include_router(review.router, prefix="/api/v1")
    app.include_router(redaction.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()
