"""SQLite 索引与「任务-结果」通用历史。

表：
  docs  —— 文档索引（doc_key、标题、catalog、语言、更新时间、本地路径、URL）
  runs  —— 通用任务执行历史（module_key 区分模块：sync / ocr / ...）
  items —— 通用结果明细（某次 run 的每条结果，detail_json 承载模块自定义结构）

设计目的：所有「定时任务 → 结果 → 展示」界面复用同一套 runs/items 模型，
新增模块（OCR 检查、链接检查等）无需迁移 schema，明细结构用 JSON 承载。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime


SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    doc_key        TEXT PRIMARY KEY,   -- "{lang}|{catalog}|{relate_document}"
    lang           TEXT NOT NULL,
    catalog        TEXT NOT NULL,
    relate_document TEXT NOT NULL,
    title          TEXT,
    file_name      TEXT,
    updated_date   TEXT,               -- updatedDate（后端字段，会批量 touch，仅参考）
    display_update_time TEXT,          -- displayUpdateTime（页面真实更新时间，增量判据）
    content_hash   TEXT,               -- HTML 内容 sha256（兜底判据）
    local_path     TEXT,
    url            TEXT,
    last_synced    TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_catalog ON docs(catalog, lang);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    module_key   TEXT NOT NULL,        -- 'sync' / 'ocr' / ...
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,                 -- running / success / failed
    summary_json TEXT,                 -- 汇总统计 JSON（模块自定义结构）
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_module ON runs(module_key, id);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    item_key    TEXT,                  -- 条目唯一标识（doc_key、图片路径等）
    item_type   TEXT,                  -- 类型（added/modified/deleted、has_cn/no_cn 等）
    detail_json TEXT,                  -- 明细内容 JSON（模块自定义 schema）
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_items_run ON items(run_id);

CREATE TABLE IF NOT EXISTS feedbacks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,         -- 反馈内容
    contact     TEXT,                  -- 联系方式（选填）
    status      TEXT DEFAULT 'new',    -- new / doing / done / ignore
    created_at  TEXT NOT NULL
);
"""


class IndexDB:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        # timeout=30：WAL 下读写不冲突，但多写者会等锁，加超时避免永久阻塞
        self._conn = sqlite3.connect(self.db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        # 迁移：旧库补 docs 列
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(docs)")]
        if "content_hash" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN content_hash TEXT")
        if "display_update_time" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN display_update_time TEXT")
        # 迁移：弃用的旧表 sync_runs / sync_changes（若为空则删除，已被 runs/items 取代）
        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for old in ("sync_runs", "sync_changes"):
            if old in tables:
                cnt = self._conn.execute(
                    f"SELECT COUNT(*) FROM {old}").fetchone()[0]
                if cnt == 0:
                    self._conn.execute(f"DROP TABLE {old}")
        self._conn.commit()

    # ---- docs 索引 ----
    def get_doc(self, doc_key: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT doc_key, lang, catalog, relate_document, title, file_name, "
            "updated_date, display_update_time, content_hash, local_path, url, "
            "last_synced FROM docs WHERE doc_key=?",
            (doc_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = ["doc_key", "lang", "catalog", "relate_document", "title",
                "file_name", "updated_date", "display_update_time", "content_hash",
                "local_path", "url", "last_synced"]
        return dict(zip(cols, row))

    def get_all_keys(self) -> set[str]:
        cur = self._conn.execute("SELECT doc_key FROM docs")
        return {r[0] for r in cur.fetchall()}

    def upsert_doc(self, d: dict):
        self._conn.execute(
            """INSERT INTO docs (doc_key, lang, catalog, relate_document, title,
               file_name, updated_date, display_update_time, content_hash,
               local_path, url, last_synced)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(doc_key) DO UPDATE SET
                 title=excluded.title, file_name=excluded.file_name,
                 updated_date=excluded.updated_date,
                 display_update_time=excluded.display_update_time,
                 content_hash=excluded.content_hash,
                 local_path=excluded.local_path,
                 url=excluded.url, last_synced=excluded.last_synced""",
            (d["doc_key"], d["lang"], d["catalog"], d["relate_document"], d.get("title"),
             d.get("file_name"), d.get("updated_date"), d.get("display_update_time"),
             d.get("content_hash"), d.get("local_path"), d.get("url"),
             d.get("last_synced")),
        )

    def delete_doc(self, doc_key: str):
        self._conn.execute("DELETE FROM docs WHERE doc_key=?", (doc_key,))

    def mark_deleted(self, doc_key: str):
        """删除索引条目（本地文件保留）。"""
        self._conn.execute("DELETE FROM docs WHERE doc_key=?", (doc_key,))

    def stats(self) -> dict:
        cur = self._conn.execute("SELECT COUNT(*) FROM docs")
        total = cur.fetchone()[0]
        cur = self._conn.execute("SELECT lang, COUNT(*) FROM docs GROUP BY lang")
        by_lang = {r[0]: r[1] for r in cur.fetchall()}
        cur = self._conn.execute("SELECT catalog, COUNT(*) FROM docs GROUP BY catalog")
        by_catalog = {r[0]: r[1] for r in cur.fetchall()}
        cur = self._conn.execute(
            "SELECT lang, catalog, COUNT(*) FROM docs GROUP BY lang, catalog")
        by_lang_catalog = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        return {"total": total, "by_lang": by_lang, "by_catalog": by_catalog,
                "by_lang_catalog": by_lang_catalog}

    # ---- runs（通用任务历史）----
    def start_run(self, module_key: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (module_key, started_at, status) VALUES (?,?, 'running')",
            (module_key, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, summary: dict, status: str = "success",
                   error: str = None):
        self._conn.execute(
            "UPDATE runs SET finished_at=?, status=?, summary_json=?, error=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), status,
             json.dumps(summary, ensure_ascii=False), error, run_id),
        )
        self._conn.commit()

    def add_item(self, run_id: int, item_key: str, item_type: str, detail: dict):
        self._conn.execute(
            "INSERT INTO items (run_id, item_key, item_type, detail_json) VALUES (?,?,?,?)",
            (run_id, item_key, item_type, json.dumps(detail, ensure_ascii=False)),
        )

    def list_runs(self, module_key: str, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            "SELECT id, module_key, started_at, finished_at, status, summary_json, error "
            "FROM runs WHERE module_key=? ORDER BY id DESC LIMIT ?",
            (module_key, limit),
        )
        result = []
        for r in cur.fetchall():
            result.append({
                "id": r[0], "module_key": r[1], "started_at": r[2],
                "finished_at": r[3], "status": r[4],
                "summary": json.loads(r[5]) if r[5] else {},
                "error": r[6],
            })
        return result

    def get_run(self, run_id: int) -> dict | None:
        cur = self._conn.execute(
            "SELECT id, module_key, started_at, finished_at, status, summary_json, error "
            "FROM runs WHERE id=?",
            (run_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": r[0], "module_key": r[1], "started_at": r[2],
            "finished_at": r[3], "status": r[4],
            "summary": json.loads(r[5]) if r[5] else {},
            "error": r[6],
        }

    def get_items(self, run_id: int) -> list[dict]:
        cur = self._conn.execute(
            "SELECT id, run_id, item_key, item_type, detail_json FROM items "
            "WHERE run_id=? ORDER BY item_type, id",
            (run_id,),
        )
        result = []
        for r in cur.fetchall():
            result.append({
                "id": r[0], "run_id": r[1], "item_key": r[2],
                "item_type": r[3],
                "detail": json.loads(r[4]) if r[4] else {},
            })
        return result

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.commit()
        self._conn.close()

    # ---- 问题反馈 ----
    def create_feedback(self, content: str, contact: str = "") -> int:
        self._conn.execute(
            "INSERT INTO feedbacks (content, contact, created_at)"
            " VALUES (?,?,?)",
            (content, contact,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self._conn.commit()
        return self._conn.execute(
            "SELECT last_insert_rowid()").fetchone()[0]

    def list_feedbacks(self, status: str | None = None) -> list[dict]:
        sql = "SELECT id, content, contact, status, created_at FROM feedbacks"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        result = []
        for r in self._conn.execute(sql, params).fetchall():
            result.append({"id": r[0], "content": r[1], "contact": r[2],
                           "status": r[3], "created_at": r[4]})
        return result

    def update_feedback_status(self, fid: int, status: str):
        self._conn.execute(
            "UPDATE feedbacks SET status=? WHERE id=?", (status, fid))
        self._conn.commit()
