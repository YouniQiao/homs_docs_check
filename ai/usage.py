"""AI 用量：opencode-go 官方额度（实时，60s 缓存）+ 本项目累计（本地 ai_cache）。

对外只给 usage_card()：返回可直接渲染的结构，模板保持“傻瓜”。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import requests

from .cache import stats
from .config import ROOT, key_fingerprint, load_config

CACHE_FILE = ROOT / ".ai_usage_cache.json"
TTL = 60          # 秒：官方额度缓存时长
_CST = timezone(timedelta(hours=8))

_LABELS = (("rolling", "滚动窗口"), ("weekly", "本周"), ("monthly", "本月"))


def _level(pct: float) -> tuple[str, str]:
    if pct >= 90:
        return "danger", "#d13438"
    if pct >= 70:
        return "warn", "#c2570a"
    return "ok", "#128c4a"


def _fmt_reset(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_CST)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return "—"


def _fmt_tokens(n: int) -> str:
    if n >= 1e8:
        return f"{n/1e8:.1f}亿"
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return str(n)


def provider_usage(force: bool = False) -> dict:
    """拉官方 /usage；60s 文件缓存；失败回退上次值并标 stale。"""
    now = time.time()
    cfg = load_config()
    fp = key_fingerprint()          # 绑定当前 key：换 key 则缓存立即作废
    cached: dict | None = None
    try:
        cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cached = None
    same_key = bool(cached) and cached.get("_fp") == fp
    if cached and same_key and not force and now - float(cached.get("_ts", 0)) < TTL:
        return cached

    out: dict = {"_ts": now, "stale": False, "error": "", "_fp": fp}
    try:
        r = requests.get(f"{cfg.base_url}/usage",
                         headers={"Authorization": f"Bearer {cfg.api_key}", **cfg.extra_headers},
                         timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        out.update(r.json().get("usage", {}) or {})
    except Exception as ex:  # noqa: BLE001
        # 只有「同一 key」才回退上次值；换了 key 宁可显示空，也不显示别的账户用量
        if cached and same_key:
            cached["stale"] = True
            return cached
        out["stale"] = True
        out["error"] = str(ex)[:120]
    try:
        CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def local_usage() -> dict:
    s = stats()
    calls = sum(v["calls"] for v in s.values())
    tin = sum(v["tokens_in"] for v in s.values())
    tout = sum(v["tokens_out"] for v in s.values())
    return {"calls": calls, "tokens_in": tin, "tokens_out": tout,
            "tokens": tin + tout, "tokens_h": _fmt_tokens(tin + tout), "kinds": s}


def usage_card() -> dict:
    """给首页渲染用的结构。"""
    p = provider_usage()
    gauges = []
    for key, label in _LABELS:
        d = p.get(key) or {}
        if not isinstance(d, dict):
            continue
        pct = d.get("percent")
        if pct is None:
            continue
        pct = float(pct)
        _lvl, color = _level(pct)
        gauges.append({"key": key, "label": label, "percent": round(pct, 1),
                       "color": color, "reset": _fmt_reset(d.get("resetsAt", "")),
                       "status": d.get("status", "")})
    return {"ok": bool(gauges), "stale": bool(p.get("stale")), "error": p.get("error", ""),
            "gauges": gauges, "local": local_usage(), "ts": time.time()}
