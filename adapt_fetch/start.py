#!/usr/bin/env python3
"""Launch Adapt-Fetch HTTP server."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    import uvicorn

    host = os.getenv("ADAPT_FETCH_HOST", "0.0.0.0")
    port = int(os.getenv("ADAPT_FETCH_PORT", "8900"))
    reload = os.getenv("ADAPT_FETCH_RELOAD", "false").lower() == "true"

    uvicorn.run(
        "adapt_fetch.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=os.getenv("APP_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
