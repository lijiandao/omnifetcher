"""FastAPI application for the Adapt-Fetch URL parsing engine."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from adapt_fetch.playwright_service.playwright_router import (
    cleanup_crawler,
    crawler,
    initialize_crawler,
    router as fetch_router,
)
from adapt_fetch.utils.logging_setup import configure_global_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_global_logging(service_name="adapt_fetch")
    await initialize_crawler()
    yield
    await cleanup_crawler()


app = FastAPI(
    title="Adapt-Fetch",
    description=(
        "Adaptive URL fetch engine: EasyGet HTTP fast path, Playwright browser path, "
        "PDF dedicated pipeline, Jina fallback, proxy rotation, and domain auto-learning."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(fetch_router, tags=["fetch"])


@app.get("/")
async def root():
    return {
        "service": "adapt-fetch",
        "version": "0.1.0",
        "endpoints": {
            "crawl": "POST /crawl",
            "health": "GET /health",
        },
    }


@app.get("/health")
async def health():
    ready = crawler is not None
    return JSONResponse(
        {
            "status": "healthy" if ready else "starting",
            "crawler_initialized": ready,
        },
        status_code=200 if ready else 503,
    )
