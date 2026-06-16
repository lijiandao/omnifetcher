# Adapt-Fetch

**Adaptive URL fetch engine** — automatically learns whether each domain should use fast HTTP, headless browser, or PDF fast path. Built for RAG pipelines and AI agents.

## Features

- **Smart routing (`SmartModeDetector`)** — domain score cache, SPA/PDF rules, auto-learning from successful fetches
- **EasyGet fast path** — pure HTTP with encoding detection, mojibake/binary guards (`ftfy`, printable-ratio)
- **Playwright path** — JS rendering, anti-bot pages, Edge persistent profile
- **PDF pipeline** — dedicated direct-download path (`EasyPDFCrawler` / `PDFCrawler`)
- **Concurrent race** — EasyGet + Playwright (+ optional Jina) with graceful cancellation
- **Proxy rotation** — Clash API integration with weighted node selection
- **Optional double-hop proxy** — local relay for upstream rotation pools
- **Huge HTML** — readability + map-reduce markdown extraction

## Quick start

```bash
cd adapt-fetch
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # or use system Edge (Windows)

# copy and customize configs
cp config/smart_detector_config.json config/smart_detector_config.local.json  # optional

python -m adapt_fetch.start
# POST http://127.0.0.1:8900/crawl
```

Example request:

```bash
curl -s -X POST http://127.0.0.1:8900/crawl \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": ["https://arxiv.org/abs/2503.21088"],
    "mode": "concurrent",
    "use_intellicache": true,
    "htmlclean_enabled": true,
    "extract_title": true
  }'
```

Or use the sample script:

```bash
python examples/fetch_one.py "https://arxiv.org/abs/2503.21088"
```

## Configuration

| Path | Purpose |
|------|---------|
| `config/smart_detector_config.json` | SPA/PDF rules + learned domain decisions |
| `config/proxy_config.yaml` | Clash / proxy pool settings |
| `config/proxy_state/` | Runtime proxy usage history (gitignored) |

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADAPT_FETCH_HOST` | `0.0.0.0` | HTTP bind host |
| `ADAPT_FETCH_PORT` | `8900` | HTTP bind port |
| `APP_LOG_LEVEL` | `INFO` | Log level |
| `DOUBLE_HOP_USER_HK` | — | 711 proxy user (HK pool) |
| `DOUBLE_HOP_USER_GLOBAL` | — | 711 proxy user (global pool) |
| `DOUBLE_HOP_PASS` | — | 711 proxy password |

## Architecture

```
URL → SmartModeDetector (cache / rules / learning)
    ├─ PDF rules → EasyPDF / PDFCrawler
    ├─ easyget cache hit → EasyGetCrawler
    ├─ playwright cache hit → Playwright
    └─ concurrent (default) → race EasyGet ∥ Playwright ∥ Jina
                              → health check → learn & persist
```

## Optional: double-hop proxy

For Jina / geo-sensitive fetches, run the local relay (requires your own upstream credentials):

```bash
export DOUBLE_HOP_USER_HK=your-user
export DOUBLE_HOP_PASS=your-pass
python -m adapt_fetch.proxy.double_hop_proxy
```

## Origin

Extracted from [LightRead](https://lightread.ai) `unified_backend` URL parsing engine. LightRead uses this stack for link attachment parsing, resource import, and agent `fetch_url` tooling.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Compliance

You are responsible for complying with target sites' terms of service and robots policies. Use reasonable rate limits and respect copyright.
