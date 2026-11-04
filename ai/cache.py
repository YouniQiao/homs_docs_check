"""AI 结果缓存（index.db 内，与 url_cache 并列）。

ai_cache ：按 cache_key(=kind|model|prompt_version|输入哈希) 缓存模型输出，未变条目永不重复调用
ai_notes ：面向页面的结论（按 module+item_key+kind 幂等覆盖），供新列/筛选 join
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .config import load_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_cache (
  cache_key      TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_hash     TEXT NOT NULL,
  output_json    TEXT NOT NULL,
  tokens_in      INTEGER DEFAULT 0,
  tokens_out     INTEGER DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_cache_kind ON ai_cache(kind);

CREATE TABLE IF NOT EXISTS ai_notes (
  module         TEXT NOT NULL,
  item_key       TEXT NOT NULL,
  kind           TEXT NOT NULL,
  verdict        TEXT,
  confidence     REAL,
  reason         TEXT,
  suggestion     TEXT,
  model          TEXT,
  prompt_version TEXT,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (module, item_key, kind)
);
CREATE INDEX IF NOT EXISTS idx_ai_notes_kind ON ai_notes(kind);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(load_config().db_path, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return c


def _write(sql: str, args: tuple) -> None:
    c = _conn()
    try:
        with c:
            c.execute(sql, args)
    finally:
        c.close()


def _query(sql: str, args: tuple = ()) -> list:
    c = _conn()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def ensure_schema() -> None:
    c = _conn()
    try:
        with c:
            c.executescript(_SCHEMA)
    finally:
        c.close()


def item_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cache_key(kind: str, model: str, pver: str, payload) -> str:
    return hashlib.sha256(
        f"{kind}|{model}|{pver}|{item_hash(payload)}".encode()).hexdigest()


def cache_get(key: str) -> dict | None:
    rows = _query("SELECT output_json FROM ai_cache WHERE cache_key=?", (key,))
    return json.loads(rows[0][0]) if rows else None


def cache_put(key: str, kind: str, model: str, pver: str, ihash: str,
              output: dict, usage: dict | None = None) -> None:
    u = usage or {}
    _write("INSERT OR REPLACE INTO ai_cache"
           "(cache_key,kind,model,prompt_version,input_hash,output_json,"
           "tokens_in,tokens_out,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
           (key, kind, model, pver, ihash, json.dumps(output, ensure_ascii=False),
            int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0), _now()))


def note_put(module: str, item_key: str, kind: str, data: dict,
             model: str, pver: str) -> None:
    _write("INSERT OR REPLACE INTO ai_notes"
           "(module,item_key,kind,verdict,confidence,reason,suggestion,model,prompt_version,updated_at)"
           " VALUES(?,?,?,?,?,?,?,?,?,?)",
           (module, item_key, kind, data.get("verdict"), data.get("confidence"),
            data.get("reason"), data.get("suggestion"), model, pver, _now()))


def note_get(module: str, kind: str = "") -> dict:
    """{item_key: {verdict, confidence, reason, suggestion}}"""
    if kind:
        rows = _query("SELECT item_key,verdict,confidence,reason,suggestion FROM ai_notes "
                      "WHERE module=? AND kind=?", (module, kind))
    else:
        rows = _query("SELECT item_key,verdict,confidence,reason,suggestion FROM ai_notes "
                      "WHERE module=?", (module,))
    return {r[0]: {"verdict": r[1], "confidence": r[2], "reason": r[3], "suggestion": r[4]}
            for r in rows}


def stats() -> dict:
    out = {}
    for k, in _query("SELECT DISTINCT kind FROM ai_cache"):
        n, tin, tout = _query(
            "SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) "
            "FROM ai_cache WHERE kind=?", (k,))[0]
        out[k] = {"calls": n, "tokens_in": tin, "tokens_out": tout}
    return out
