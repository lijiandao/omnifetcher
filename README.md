<div align="center">

<img src="docs/assets/omnifetcher-hero.png" alt="OmniFetcher banner" width="920" />

# OmniFetcher

### AI Agent Network Base

**Adaptive URL fetch engine for agents & RAG — learns the best route per domain (HTTP · Browser · PDF).**

<br />

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-HTTP%20API-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-browser%20path-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](https://github.com/lijiandao/omnifetcher/pulls)

[English](README.md) · [中文](README.zh-CN.md)

<br />

[Quick Start](#-quick-start) · [Benchmarks](#-benchmarks) · [Architecture](#-architecture) · [Configuration](#-configuration)

</div>

---

## ✨ Highlights

<table>
<tr>
<td width="50%">

**🧠 Self-learning router**

`SmartModeDetector` caches per-domain decisions, discovers PDF patterns, and improves after every successful fetch — not a static rules file.

</td>
<td width="50%">

**⚡ Multi-lane race**

EasyGet (HTTP) · Playwright (JS) · PDF fast path · optional Jina fallback — concurrent with graceful cancellation.

</td>
</tr>
<tr>
<td>

**🛡️ Content quality guards**

Encoding detection, mojibake checks (`ftfy`), binary guards, Cloudflare / captcha heuristics.

</td>
<td>

**🌐 Agent-ready network layer**

Proxy rotation (Clash), optional double-hop relay, huge-HTML readability map-reduce → clean Markdown.

</td>
</tr>
</table>

---

## 📊 Benchmarks

> Measured in maintainer test environment (June 2026). Competitor columns are **third-party fetch/crawl APIs** under the same URLs. Your mileage may vary — run [`benchmarks/run_benchmark.py`](benchmarks/run_benchmark.py) locally to reproduce OmniFetcher numbers.

<img src="docs/assets/omnifetcher-benchmark.png" alt="Benchmark overview" width="720" />

| Scenario | URL type | **OmniFetcher** | Tavily | Exa | Generic reader API |
|:--|:--|--:|--:|--:|--:|
| arXiv 9-page PDF | Direct PDF | **0.8 s** ✅ | ~3 s (cached) | 1.78 s | 2.8 s |
| arXiv ~300-page PDF | Large PDF | **3.3 s** ✅ | — | 4 s timeout ❌ | 25 s fail ❌ |
| Zhihu Q&A | Anti-bot SPA | **1.6 s** ✅ | 6.9 s blocked ❌ | 4 s timeout ❌ | 5.5 s empty ❌ |
| Juejin article | Static HTML + MD cleanup | **435 ms** ✅ | — | 4 s timeout ❌ | 700 ms “Please wait” ❌ |

**Cold → warm learning** (same domain, repeated fetches):

| Visit | Avg decision time | Notes |
|--:|--:|:--|
| 1st | ~10 s | Probe + concurrent race |
| 3rd | ~3 s | Domain score accumulating |
| 5th+ | **~1 s** | Cache hit on optimal lane |

```bash
# Reproduce OmniFetcher column locally
python benchmarks/run_benchmark.py
python benchmarks/run_benchmark.py --url "https://arxiv.org/pdf/2503.21088"
```

---

## 🚀 Quick start

```bash
git clone https://github.com/lijiandao/omnifetcher.git
cd omnifetcher

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # Linux; Windows can use Edge profile

python -m omnifetcher.start
# → http://127.0.0.1:8900
```

**Fetch a URL**

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

Or:

```bash
python examples/fetch_one.py "https://arxiv.org/abs/2503.21088"
```

---

## 🏗 Architecture

<img src="docs/assets/omnifetcher-architecture.png" alt="Architecture diagram" width="720" />

```mermaid
flowchart LR
  A[URL] --> B{SmartModeDetector}
  B -->|PDF rule| C[PDF Fast Path]
  B -->|cached easyget| D[EasyGet HTTP]
  B -->|cached browser| E[Playwright]
  B -->|unknown| F[Concurrent Race]
  F --> D
  F --> E
  F --> G[Jina Fallback]
  D --> H[Health Check]
  E --> H
  G --> H
  C --> H
  H --> I[Markdown / Metadata]
  H --> J[(Learn & Persist)]
```

| Module | Role |
|:--|:--|
| `SmartModeDetector` | SPA/PDF rules, domain score cache, auto-learning |
| `EasyGetCrawler` | Fast HTTP, encoding & garbled-text detection |
| `PlaywrightCrawler` | JS rendering, anti-bot, Edge persistent context |
| `EasyPDFCrawler` | Direct PDF download & text extraction |
| `concurrent_strategies` | EasyGet ∥ Playwright ∥ Jina race + cancel |
| `proxy/` | Clash rotation, weighted node selection, double-hop relay |
| `tackle_huge_html` | Readability + map-reduce for large pages |

---

## ⚙️ Configuration

| Path | Purpose |
|:--|:--|
| `config/smart_detector_config.json` | SPA/PDF rules + learned domain decisions |
| `config/proxy_config.yaml` | Clash / proxy pool settings |
| `config/proxy_state/` | Runtime proxy usage history *(gitignored)* |

| Variable | Default | Description |
|:--|:--|:--|
| `OMNIFETCHER_HOST` | `0.0.0.0` | HTTP bind host |
| `OMNIFETCHER_PORT` | `8900` | HTTP bind port |
| `OMNIFETCHER_BASE` | `http://127.0.0.1:8900` | Client base URL for examples |
| `APP_LOG_LEVEL` | `INFO` | Log level |
| `DOUBLE_HOP_USER_HK` | — | Upstream proxy user (HK pool) |
| `DOUBLE_HOP_USER_GLOBAL` | — | Upstream proxy user (global pool) |
| `DOUBLE_HOP_PASS` | — | Upstream proxy password |

<details>
<summary><b>Optional: double-hop proxy</b></summary>

<br />

For geo-sensitive fetches, run the local relay with your own upstream credentials:

```bash
export DOUBLE_HOP_USER_HK=your-user
export DOUBLE_HOP_PASS=your-pass
python -m omnifetcher.proxy.double_hop_proxy
```

</details>

---

## 📦 Install as CLI

```bash
pip install -e .
omnifetcher   # same as python -m omnifetcher.start
```

---

## 🤝 Contributing

Issues and PRs welcome. Please include repro URLs when reporting fetch failures.

---

## 📄 License

[Apache License 2.0](LICENSE)

---

## ⚠️ Compliance

You are responsible for complying with target sites' terms of service and robots policies. Use reasonable rate limits and respect copyright.

<div align="center">
<sub>Built for agents that need the web — fast, clean, and smarter every run.</sub>
</div>
