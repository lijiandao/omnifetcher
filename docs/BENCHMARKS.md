# Benchmark Gallery

> Visual evidence from controlled tests (June 2025–2026).  
> [中文文档](BENCHMARKS.zh-CN.md)

Each scenario uses the **same URL** across OmniFetcher and third-party fetch APIs.

---

## 1 · arXiv 9-page PDF

**URL:** `https://arxiv.org/pdf/2503.21088`

| Engine | Result | Time |
|:--|:--|--:|
| **OmniFetcher** | Full PDF → Markdown | **791 ms** |
| Metaso Reader API | Success | 2.8 s |
| Exa Extract | Success | ~1.8 s |

<table>
<tr>
<td width="33%" align="center"><b>OmniFetcher</b><br/><img src="assets/benchmarks/feishu/bench_01.png" width="100%"/></td>
<td width="33%" align="center"><b>Metaso Reader</b><br/><img src="assets/benchmarks/feishu/bench_02.png" width="100%"/></td>
<td width="33%" align="center"><b>Exa Extract</b><br/><img src="assets/benchmarks/feishu/bench_03.png" width="100%"/></td>
</tr>
</table>

---

## 2 · arXiv ~300-page PDF (10 MB+)

**URL:** `https://arxiv.org/pdf/2106.05764`

| Engine | Result | Time |
|:--|:--|--:|
| **OmniFetcher** | Full TOC + text as Markdown | **3.24 s** |
| Metaso Reader API | No data returned | 25.5 s |
| Exa Extract | `CRAWL_LIVECRAWL_TIMEOUT` | 4.03 s |
| Tavily Extract | Partial snippet only | ~3 s |

<table>
<tr>
<td width="25%" align="center"><b>OmniFetcher</b><br/><img src="assets/benchmarks/feishu/bench_04.png" width="100%"/></td>
<td width="25%" align="center"><b>Metaso</b><br/><img src="assets/benchmarks/feishu/bench_05.png" width="100%"/></td>
<td width="25%" align="center"><b>Exa</b><br/><img src="assets/benchmarks/feishu/bench_06.png" width="100%"/></td>
<td width="25%" align="center"><b>Tavily</b><br/><img src="assets/benchmarks/feishu/bench_07.png" width="100%"/></td>
</tr>
</table>

---

## 3 · Zhihu (anti-bot SPA)

**URL:** `https://www.zhihu.com/question/563026612`

| Engine | Result | Time |
|:--|:--|--:|
| **OmniFetcher** | Clean Markdown (632 chars) | **1.61 s** |
| Metaso Reader API | No relevant data | 5.5 s |
| Tavily Extract | Access denied | 0.33 s |
| Exa Extract | `CRAWL_LIVECRAWL_TIMEOUT` | 4.05 s |

<table>
<tr>
<td width="25%" align="center"><b>OmniFetcher</b><br/><img src="assets/benchmarks/feishu/bench_08.png" width="100%"/></td>
<td width="25%" align="center"><b>Metaso</b><br/><img src="assets/benchmarks/feishu/bench_09.png" width="100%"/></td>
<td width="25%" align="center"><b>Tavily</b><br/><img src="assets/benchmarks/feishu/bench_10.png" width="100%"/></td>
<td width="25%" align="center"><b>Exa</b><br/><img src="assets/benchmarks/feishu/bench_11.png" width="100%"/></td>
</tr>
</table>

---

## 4 · Juejin article (static HTML + MD cleanup)

**URL:** `https://juejin.cn/post/7220972390283788345`

| Engine | Result | Time |
|:--|:--|--:|
| **OmniFetcher** | HTML fetch + Readability → Markdown | **435 ms** |
| Metaso Reader API | “Please wait…” gate page | 0.7 s |
| Exa Extract | `CRAWL_LIVECRAWL_TIMEOUT` | 4.02 s |

<table>
<tr>
<td width="33%" align="center"><b>OmniFetcher</b><br/><img src="assets/benchmarks/feishu/bench_14.png" width="100%"/></td>
<td width="33%" align="center"><b>Metaso</b><br/><img src="assets/benchmarks/feishu/bench_12.png" width="100%"/></td>
<td width="33%" align="center"><b>Exa</b><br/><img src="assets/benchmarks/feishu/bench_13.png" width="100%"/></td>
</tr>
</table>

> OmniFetcher timing includes **network + HTML cleanup + Markdown extraction**, not raw HTTP alone.

---

## 5 · PDF & academic pages (terminal logs)

Additional PDF / OpenReview runs from the same test suite:

<table>
<tr>
<td width="33%" align="center"><b>OpenReview (1.07 s)</b><br/><img src="assets/benchmarks/feishu/bench_15.png" width="100%"/></td>
<td width="33%" align="center"><b>arXiv PDF 0.85 MB (0.97 s)</b><br/><img src="assets/benchmarks/feishu/bench_16.png" width="100%"/></td>
<td width="33%" align="center"><b>OpenReview (0.77 s)</b><br/><img src="assets/benchmarks/feishu/bench_17.png" width="100%"/></td>
</tr>
</table>

---

## Reproduce locally

```bash
# Start server
python -m omnifetcher.start

# Run bundled benchmark script
python benchmarks/run_benchmark.py
python benchmarks/run_benchmark.py --url "https://arxiv.org/pdf/2503.21088"
```

---

## Notes

- Third-party API names refer to publicly available reader/crawl services tested under the same URLs.
- Screenshots are from internal QA runs; competitor UIs may change over time.
- Always respect target site ToS and rate limits when re-running tests.
