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

> Measured in maintainer test environment (2025–2026). Full screenshot gallery: **[📷 Benchmark Gallery (EN)](docs/BENCHMARKS.md)** · **[📷 性能对比图集 (中文)](docs/BENCHMARKS.zh-CN.md)**

<img src="docs/assets/omnifetcher-benchmark.png" alt="Benchmark overview" width="720" />

| Scenario | URL type | **OmniFetcher** | Tavily | Exa | Metaso / Reader API |
|:--|:--|--:|--:|--:|--:|
| arXiv 9-page PDF | Direct PDF | **791 ms** ✅ | ~3 s (cached est.) | ~1.8 s | 2.8 s |
| arXiv ~300-page PDF | Large PDF | **3.24 s** ✅ | partial | 4 s timeout ❌ | 25.5 s fail ❌ |
| Zhihu Q&A | Anti-bot SPA | **1.61 s** ✅ | access denied ❌ | 4 s timeout ❌ | 5.5 s empty ❌ |
| Juejin article | HTML + MD cleanup | **435 ms** ✅ | — | 4 s timeout ❌ | 0.7 s gate page ❌ |

<details open>
<summary><b>📸 arXiv 9-page PDF — side-by-side screenshots</b></summary>
<br />
<table>
<tr>
<td width="33%" align="center"><b>OmniFetcher · 791 ms</b><br/><img src="docs/assets/benchmarks/feishu/bench_01.png" width="100%"/></td>
<td width="33%" align="center"><b>Metaso · 2.8 s</b><br/><img src="docs/assets/benchmarks/feishu/bench_02.png" width="100%"/></td>
<td width="33%" align="center"><b>Exa · ~1.8 s</b><br/><img src="docs/assets/benchmarks/feishu/bench_03.png" width="100%"/></td>
</tr>
</table>
</details>

<details>
<summary><b>📸 arXiv 300-page PDF — OmniFetcher vs competitors</b></summary>
<br />
<table>
<tr>
<td width="25%" align="center"><b>OmniFetcher · 3.24 s</b><br/><img src="docs/assets/benchmarks/feishu/bench_04.png" width="100%"/></td>
<td width="25%" align="center"><b>Metaso · fail</b><br/><img src="docs/assets/benchmarks/feishu/bench_05.png" width="100%"/></td>
<td width="25%" align="center"><b>Exa · timeout</b><br/><img src="docs/assets/benchmarks/feishu/bench_06.png" width="100%"/></td>
<td width="25%" align="center"><b>Tavily · partial</b><br/><img src="docs/assets/benchmarks/feishu/bench_07.png" width="100%"/></td>
</tr>
</table>
</details>

<details>
<summary><b>📸 Zhihu anti-bot page</b></summary>
<br />
<table>
<tr>
<td width="25%" align="center"><b>OmniFetcher · 1.61 s</b><br/><img src="docs/assets/benchmarks/feishu/bench_08.png" width="100%"/></td>
<td width="25%" align="center"><b>Metaso · 5.5 s</b><br/><img src="docs/assets/benchmarks/feishu/bench_09.png" width="100%"/></td>
<td width="25%" align="center"><b>Tavily · denied</b><br/><img src="docs/assets/benchmarks/feishu/bench_10.png" width="100%"/></td>
<td width="25%" align="center"><b>Exa · timeout</b><br/><img src="docs/assets/benchmarks/feishu/bench_11.png" width="100%"/></td>
</tr>
</table>
</details>

<details>
<summary><b>📸 Juejin article (includes Markdown cleanup)</b></summary>
<br />
<table>
<tr>
<td width="33%" align="center"><b>OmniFetcher · 435 ms</b><br/><img src="docs/assets/benchmarks/feishu/bench_14.png" width="100%"/></td>
<td width="33%" align="center"><b>Metaso · gate page</b><br/><img src="docs/assets/benchmarks/feishu/bench_12.png" width="100%"/></td>
<td width="33%" align="center"><b>Exa · timeout</b><br/><img src="docs/assets/benchmarks/feishu/bench_13.png" width="100%"/></td>
</tr>
</table>
</details>

**Cold → warm learning** (same domain, repeated fetches):

| Visit | Avg decision time | Notes |
|--:|--:|:--|
| 1st | ~10 s | Probe + concurrent race |
| 3rd | ~3 s | Domain score accumulating |
| 5th+ | **~1 s** | Cache hit on optimal lane |

```bash
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
