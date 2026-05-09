"""FastAPI app with embedded background ingestion worker."""
import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.config import get_settings
from app.db import close_pool, init_pool
from app.embed import load_model
from app.ingest import worker_loop
from app.routes import router


def _configure_logging() -> None:
    level = get_settings().LOG_LEVEL.upper()
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    log = structlog.get_logger("startup")
    settings = get_settings()
    await init_pool()
    log.info("startup.db_ready")
    # Pre-load embedding model so first ingest isn't a 30s stall.
    load_model()
    log.info("startup.embed_ready", model=settings.EMBED_MODEL)

    stop_event = asyncio.Event()
    workers = [
        asyncio.create_task(worker_loop(stop_event), name=f"worker-{i}")
        for i in range(settings.WORKER_CONCURRENCY)
    ]
    log.info("startup.workers_started", count=len(workers))
    try:
        yield
    finally:
        log.info("shutdown.stopping_workers")
        stop_event.set()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="rag-platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
