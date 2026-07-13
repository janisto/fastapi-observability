"""Runnable AWS FastAPI application."""

import logging
import os

from fastapi import FastAPI

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContextMiddleware,
)


def env_or_default(name: str, fallback: str) -> str:
    """Return a non-empty environment value or its fallback."""
    return os.getenv(name) or fallback


def project_fields() -> dict[str, str]:
    """Return stable fields shared by application and access logs."""
    return {
        "service": env_or_default("SERVICE_NAME", "fastapi-example"),
        "environment": env_or_default("SERVICE_ENV", "local"),
        "version": env_or_default("SERVICE_VERSION", "dev"),
        "cloud_provider": "aws",
        "cloud_region": os.getenv("AWS_REGION", ""),
    }


def configure_logging() -> None:
    """Configure AWS-compatible JSON logging."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter(LoggingPreset.AWS))
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def create_app() -> FastAPI:
    """Create the example application."""
    configure_logging()
    logger = logging.LoggerAdapter(logging.getLogger(__name__), project_fields(), merge_extra=True)
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(
        AccessLogMiddleware,
        config=AccessLogConfig(
            logger=logging.getLogger("http.access"),
            preset=LoggingPreset.AWS,
            extra_fields=lambda _scope: project_fields(),
        ),
    )
    fastapi_app.add_middleware(RequestContextMiddleware)

    @fastapi_app.get("/health", operation_id="health_check")
    async def health() -> dict[str, bool]:
        """Return service health."""
        logger.info("health check", extra={"component": "health"})
        return {"ok": True}

    return fastapi_app


app = create_app()
