#!/usr/bin/env python3
"""系统合并整改 — 独立的忽略库（用户要求：不放进 index.db）。

表结构与 index.db 的 ignores 对齐，便于复用 ignores.py 的逻辑。
粒度：target = 文档 doc_key（或 "*" 表示整词忽略），kind = 匹配词。
"""
from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
DB_PATH = HERE / "ignore.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ignores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module_key  TEXT NOT NULL,
    target      TEXT NOT NULL,
    doc_key     TEXT DEFAULT '',
    kind        TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_by  TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    created_ip  TEXT DEFAULT '',
    restored_at TEXT DEFAULT '',
    restored_ip TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ign_active ON ignores(module_key, restored_at);
"""


class IgnoreStore:
    """轻量封装；方法名与 IndexDB 的忽略方法一致，便于当作 ignores.py 的后端。"""

    def __init__(self, path: str | pathlib.Path = DB_PATH):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ---- 与 IndexDB 对齐的接口 ----
    def active_ignore_map(self) -> dict:
        rows = self._conn.execute(
            "SELECT module_key,target,doc_key,kind FROM ignores "
            "WHERE restored_at IS NULL OR restored_at=''").fetchall()
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["module_key"], []).append(
                {"target": r["target"], "doc_key": r["doc_key"], "kind": r["kind"]})
        return out

    def add_ignore(self, module_key: str, target: str, kind: str,
                   doc_key: str = "", reason: str = "", ip: str = "",
                   created_by: str = "") -> bool:
        """已存在生效记录则返回 False（幂等）。"""
        cur = self._conn.execute(
            "SELECT 1 FROM ignores WHERE module_key=? AND target=? AND kind=? "
            "AND (restored_at IS NULL OR restored_at='')",
            (module_key, target, kind))
        if cur.fetchone():
            return False
        self._conn.execute(
            "INSERT INTO ignores (module_key,target,doc_key,kind,reason,"
            "created_by,created_at,created_ip) VALUES (?,?,?,?,?,?,?,?)",
            (module_key, target, doc_key, kind, reason, created_by,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip))
        self._conn.commit()
        return True

    def restore_ignores_for(self, module_key: str, target: str, kind: str,
                            doc_key: str = "", ip: str = "") -> int:
        """恢复：精确 target，或该 kind 的整词忽略（target='*'）。"""
        cur = self._conn.execute(
            "UPDATE ignores SET restored_at=?, restored_ip=? "
            "WHERE module_key=? AND target IN (?, '*') AND kind=? "
            "AND (restored_at IS NULL OR restored_at='')",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip,
             module_key, target, kind))
        self._conn.commit()
        return cur.rowcount

    def all_records(self, only_active: bool = False) -> list[dict]:
        q = "SELECT * FROM ignores"
        if only_active:
            q += " WHERE restored_at IS NULL OR restored_at=''"
        q += " ORDER BY id DESC"
        return [dict(r) for r in self._conn.execute(q).fetchall()]
