import os
import json
import time
import random
from typing import Dict, Any, List, Tuple


def load_usage_history(filepath: str) -> Dict[str, Any]:
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_usage_history(filepath: str, history: Dict[str, Any]):
    try:
        parent = os.path.dirname(filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record_usage(filepath: str, proxy_name: str):
    history = load_usage_history(filepath)
    now_ts = int(time.time())
    entry = history.get(proxy_name, {"last_used": 0, "count": 0})
    entry["last_used"] = now_ts
    entry["count"] = int(entry.get("count", 0)) + 1
    history[proxy_name] = entry
    save_usage_history(filepath, history)


def select_with_weight(
    available_proxies: List[str],
    current_proxy: str,
    delays: Dict[str, int],
    usage_history_path: str,
    weight_delay: float,
    cooldown_seconds: int,
    selection_topk: int,
) -> str:
    """
    评分= delay_norm * weight_delay + recency_norm * (1-weight_delay)
    - delay_norm: min-max 归一，越低延迟越接近1
    - recency_norm: 最近使用时间的归一，越久未使用越接近1
    """
    candidates = [p for p in available_proxies if p != current_proxy and delays.get(p, -1) > 0]
    if not candidates:
        return None

    delay_values = [delays[p] for p in candidates]
    min_delay = min(delay_values)
    max_delay = max(delay_values)
    span = max(1, max_delay - min_delay)

    usage_history = load_usage_history(usage_history_path)
    now_ts = int(time.time())

    scored: List[Tuple[str, float]] = []
    for proxy in candidates:
        d_ms = delays.get(proxy, 0)
        delay_norm = (max_delay - d_ms) / span if span > 0 else 1.0

        last_used = 0
        if isinstance(usage_history.get(proxy), dict):
            last_used = int(usage_history.get(proxy, {}).get("last_used", 0))
        delta = max(0, now_ts - last_used)
        recency_norm = 1.0 if last_used == 0 else min(1.0, delta / max(1, cooldown_seconds))

        score = weight_delay * delay_norm + (1.0 - weight_delay) * recency_norm
        scored.append((proxy, score))

    if not scored:
        return None

    scored.sort(key=lambda x: x[1], reverse=True)
    topk = max(1, min(selection_topk, len(scored)))
    return random.choice(scored[:topk])[0]


