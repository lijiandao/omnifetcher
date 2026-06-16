#!/usr/bin/env python3
"""Reproduce OmniFetcher benchmark table against public fetch APIs.

Usage:
  python benchmarks/run_benchmark.py
  python benchmarks/run_benchmark.py --url https://arxiv.org/pdf/2503.21088

Requires:
  - OmniFetcher running at OMNIFETCHER_BASE (default http://127.0.0.1:8900)
  - Optional third-party API keys via env vars for comparison columns
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional


OMNIFETCHER_BASE = os.getenv("OMNIFETCHER_BASE", "http://127.0.0.1:8900")

DEFAULT_CASES = [
    {
        "name": "arXiv 9-page PDF",
        "url": "https://arxiv.org/pdf/2503.21088",
    },
    {
        "name": "arXiv 300-page PDF",
        "url": "https://arxiv.org/pdf/2106.05764",
    },
    {
        "name": "Zhihu (anti-bot page)",
        "url": "https://www.zhihu.com/question/563026612",
    },
    {
        "name": "Juejin article (static HTML)",
        "url": "https://juejin.cn/post/7220972390283788345",
    },
]


@dataclass
class BenchResult:
    ok: bool
    elapsed_ms: int
    note: str = ""


def _post_json(url: str, payload: dict, timeout: float) -> tuple[dict, int]:
    started = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return body, elapsed_ms


def bench_omnifetcher(url: str) -> BenchResult:
    try:
        body, elapsed_ms = _post_json(
            f"{OMNIFETCHER_BASE.rstrip('/')}/crawl",
            {
                "urls": [url],
                "mode": "concurrent",
                "use_intellicache": True,
                "htmlclean_enabled": True,
                "extract_title": True,
                "timeout": 60000,
            },
            timeout=120,
        )
        data = body.get("data") or {}
        results = data.get("results") or []
        first = results[0] if results else {}
        ok = bool(first.get("success"))
        text_len = first.get("text_length") or len((first.get("markdown") or ""))
        note = f"text={text_len}"
        if not ok:
            note = first.get("playwright_error") or first.get("easyget_error") or "failed"
        return BenchResult(ok=ok, elapsed_ms=elapsed_ms, note=str(note)[:80])
    except Exception as exc:
        return BenchResult(ok=False, elapsed_ms=-1, note=str(exc)[:80])


def bench_optional(name: str, fn: Callable[[str], BenchResult]) -> Optional[Callable[[str], BenchResult]]:
    return fn if os.getenv(f"BENCH_ENABLE_{name.upper()}", "").lower() in {"1", "true", "yes"} else None


def print_table(rows: list[dict]) -> None:
    headers = ["Case", "OmniFetcher"]
    optional_cols = sorted({k for row in rows for k in row.keys() if k not in {"case", "omnifetcher"}})
    headers.extend(optional_cols)
    widths = {h: max(len(h), *(len(str(r.get(h.lower(), r.get(h, "")))) for r in rows)) for h in headers}

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(c.ljust(widths[h]) for c, h in zip(cells, headers))

    print(fmt_row(headers))
    print("-+-".join("-" * widths[h] for h in headers))
    for row in rows:
        cells = [row["case"], row["omnifetcher"]]
        for col in optional_cols:
            cells.append(row.get(col, "—"))
        print(fmt_row(cells))


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniFetcher benchmark runner")
    parser.add_argument("--url", action="append", dest="urls", help="Custom URL to test")
    args = parser.parse_args()

    cases = DEFAULT_CASES
    if args.urls:
        cases = [{"name": u[:48], "url": u} for u in args.urls]

    print(f"OmniFetcher base: {OMNIFETCHER_BASE}\n")
    rows: list[dict] = []
    for case in cases:
        result = bench_omnifetcher(case["url"])
        status = f"{result.elapsed_ms} ms ✓" if result.ok else f"FAIL ({result.note})"
        rows.append({"case": case["name"], "omnifetcher": status})
        print(f"• {case['name']}: {status}")

    print("\nSummary")
    print_table(rows)
    print(
        "\nTip: set BENCH_ENABLE_TAVILY=1 etc. after wiring your own comparator hooks;"
        " this script ships OmniFetcher numbers only to keep the repo vendor-neutral."
    )


if __name__ == "__main__":
    main()
