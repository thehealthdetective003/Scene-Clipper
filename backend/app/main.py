"""FastAPI application factory.

Startup refuses to proceed when the deployment is not safely configured
(spec 7.5). CORS is intentionally absent: the frontend and API are served from
one origin through the reverse proxy (spec 10.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import errors
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.config import ConfigurationError, get_settings
from app.logging_setup import configure_logging, get_logger

API_PREFIX = "/api/v1"

logger = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_directories()
    logger.info(
        "api starting",
        extra={
            "context": {
                "dataDir": str(settings.data_dir),
                "httpsEnabled": settings.https_enabled,
                "retentionEnabled": settings.retention_enabled,
            }
        },
    )
    yield
    logger.info("api stopping")


def create_app() -> FastAPI:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        # Fail loudly and immediately rather than serving in an unsafe state.
        raise SystemExit(str(exc)) from exc

    configure_logging(settings.log_level)

    app = FastAPI(
        title="Long-Form Video Scene Clipper",
        version="1.0.0",
        lifespan=lifespan,
        # The interactive docs would expose request shapes for key routes;
        # this is a private single-tenant deployment and does not need them.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=1_048_576,
        exempt_suffixes=("/chunks",),
    )
    app.add_middleware(RequestContextMiddleware)

    app.add_exception_handler(errors.AppError, errors.app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, errors.validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, errors.unhandled_exception_handler)

    from app.api import (
        routes_auth,
        routes_exports,
        routes_jobs,
        routes_settings,
        routes_uploads,
    )

    for router in (
        routes_auth.router,
        routes_settings.router,
        routes_uploads.router,
        routes_jobs.router,
        routes_exports.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
