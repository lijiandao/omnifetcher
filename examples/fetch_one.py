#!/usr/bin/env python3
"""Minimal example: call Adapt-Fetch HTTP API."""

import json
import os
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "https://arxiv.org/abs/2503.21088"
BASE = os.getenv("ADAPT_FETCH_BASE", "http://127.0.0.1:8900")

payload = {
    "urls": [URL],
    "timeout": 30000,
    "mode": "concurrent",
    "use_intellicache": True,
    "htmlclean_enabled": True,
    "extract_title": True,
}

req = urllib.request.Request(
    f"{BASE}/crawl",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=120) as resp:
    body = json.loads(resp.read().decode("utf-8"))

print(json.dumps(body, ensure_ascii=False, indent=2)[:4000])
