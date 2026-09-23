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
import time
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
    last_synced    TEXT,
    kit            TEXT,               -- 所属 Kit（目录树推导，缺省 NULL）
    ide            TEXT                -- IDE 分组（relate_document 以 ide- 开头，缺省 NULL）
);
CREATE INDEX IF NOT EXISTS idx_docs_catalog ON docs(catalog, lang);
-- 注意：idx_docs_kit / idx_docs_ide 不在本 SCHEMA 里建，而在 __init__ 的迁移补列之后建：
-- 旧库的 docs 还没有 kit/ide 列，写在 SCHEMA 里会让 executescript 抛 "no such column: kit"。

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

CREATE TABLE IF NOT EXISTS url_cache (
    url         TEXT PRIMARY KEY,
    status      TEXT,                  -- HTTP 状态码或 ERR:xxx
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS subscribers (
    email      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status     TEXT DEFAULT 'active'   -- active / unsubscribed
);

-- GitCode OAuth 登录用户（P1：仅身份 + 头像；关注领域/问题列表等后续阶段再挂）。
-- 用 gitcode_id（GitCode 用户 id，字符串）做唯一键：login 可改名，id 不变。
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    gitcode_id    TEXT UNIQUE NOT NULL,
    login         TEXT NOT NULL,
    name          TEXT,
    avatar_url    TEXT,
    email         TEXT,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- 站点锚点（源 HTML 的 id=，原样保存）：链接锚点校验的权威依据。
-- md 标题反推不可靠：站点锚点是 HTML 里的 id，可能挂在 <div class="section"> 上、
-- 且文字可能与当前标题不一致（标题改过 / id 建库时生成）。同步时顺手写入。
CREATE TABLE IF NOT EXISTS doc_anchors (
    doc_key TEXT NOT NULL,             -- "{lang}|{catalog}|{relate_document}"
    anchor  TEXT NOT NULL,
    PRIMARY KEY (doc_key, anchor)
);

-- 忽略记录：把"某些检测结果"标为不看（永久，手动恢复）。
-- target：linkcheck=链接 URL；encheck=doc_key；ocr/imgnorm=图片相对路径。
-- doc_key：仅 linkcheck 的"仅本文档"忽略使用；'' = 不限文档（全局）。
-- 恢复不物理删除（restored_at 标记），保留历史；无鉴权，用 *_ip 留痕。
CREATE TABLE IF NOT EXISTS ignores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module_key  TEXT NOT NULL,
    target      TEXT NOT NULL,
    doc_key     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    created_ip  TEXT NOT NULL DEFAULT '',
    restored_at TEXT,
    restored_ip TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ignores_active
    ON ignores(module_key, target, doc_key, kind) WHERE restored_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ignores_lookup ON ignores(module_key, target);
"""


class IndexDB:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        # timeout=30：WAL 下读写不冲突，但多写者会等锁，加超时避免永久阻塞
        self._conn = sqlite3.connect(self.db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=120000")
        self._conn.executescript(SCHEMA)
        # 迁移：旧库补 docs 列
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(docs)")]
        if "content_hash" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN content_hash TEXT")
        if "display_update_time" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN display_update_time TEXT")
        if "kit" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN kit TEXT")
        if "ide" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN ide TEXT")
        # 索引在补列之后建（旧库此时才有 kit/ide 列）
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_kit ON docs(kit)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_ide ON docs(ide)")
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
            "last_synced, kit, ide FROM docs WHERE doc_key=?",
            (doc_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = ["doc_key", "lang", "catalog", "relate_document", "title",
                "file_name", "updated_date", "display_update_time", "content_hash",
                "local_path", "url", "last_synced", "kit", "ide"]
        return dict(zip(cols, row))

    def get_all_keys(self) -> set[str]:
        cur = self._conn.execute("SELECT doc_key FROM docs")
        return {r[0] for r in cur.fetchall()}

    def upsert_doc(self, d: dict):
        self._conn.execute(
            """INSERT INTO docs (doc_key, lang, catalog, relate_document, title,
               file_name, updated_date, display_update_time, content_hash,
               local_path, url, last_synced, kit, ide)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(doc_key) DO UPDATE SET
                 title=excluded.title, file_name=excluded.file_name,
                 updated_date=excluded.updated_date,
                 display_update_time=excluded.display_update_time,
                 content_hash=excluded.content_hash,
                 local_path=excluded.local_path,
                 url=excluded.url, last_synced=excluded.last_synced,
                 kit=excluded.kit, ide=excluded.ide""",
            (d["doc_key"], d["lang"], d["catalog"], d["relate_document"], d.get("title"),
             d.get("file_name"), d.get("updated_date"), d.get("display_update_time"),
             d.get("content_hash"), d.get("local_path"), d.get("url"),
             d.get("last_synced"), d.get("kit"), d.get("ide")),
        )

    def delete_doc(self, doc_key: str):
        self._conn.execute("DELETE FROM docs WHERE doc_key=?", (doc_key,))

    # ── 站点锚点（源 HTML 的 id）────────────────────────────────────────
    def set_doc_anchors(self, doc_key: str, anchors):
        """整体替换某文档的锚点集（同步时用）。"""
        self._conn.execute("DELETE FROM doc_anchors WHERE doc_key=?", (doc_key,))
        rows = [(doc_key, a) for a in sorted(set(anchors or ()))]
        if rows:
            self._conn.executemany(
                "INSERT OR IGNORE INTO doc_anchors(doc_key, anchor) VALUES(?,?)", rows)

    def get_doc_anchors(self, doc_key: str) -> set:
        cur = self._conn.execute("SELECT anchor FROM doc_anchors WHERE doc_key=?", (doc_key,))
        return {r[0] for r in cur.fetchall()}

    def get_doc_anchors_map(self, keys) -> dict:
        """批量取多篇文档的锚点集，返回 {doc_key: set}（缺失的为空集）。"""
        keys = list(keys)
        out = {k: set() for k in keys}
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            q = ("SELECT doc_key, anchor FROM doc_anchors WHERE doc_key IN (%s)"
                 % ",".join("?" * len(chunk)))
            for dk, a in self._conn.execute(q, chunk):
                out[dk].add(a)
        return out

    # ── 忽略（把检测结果标为不看；恢复只标记、不删记录）──────────────
    def add_ignore(self, module_key: str, target: str, kind: str, doc_key: str = "",
                   reason: str = "", ip: str = "") -> bool:
        """新增忽略；已存在生效中的同键记录则跳过。返回是否新增。"""
        cur = self._exec_retry(
            "INSERT OR IGNORE INTO ignores"
            " (module_key, target, doc_key, kind, reason, created_at, created_ip)"
            " VALUES (?,?,?,?,?,?,?)",
            (module_key, target, doc_key, kind, reason or "",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip or ""))
        self.commit()
        return bool(cur is not None and cur.rowcount)

    def restore_ignore(self, ignore_id: int, ip: str = "") -> bool:
        """恢复单条忽略（标记 restored_at，不物理删除）。"""
        cur = self._exec_retry(
            "UPDATE ignores SET restored_at=?, restored_ip=?"
            " WHERE id=? AND restored_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip or "", ignore_id))
        self.commit()
        return bool(cur is not None and cur.rowcount)

    def restore_ignores_for(self, module_key: str, target: str, kind: str,
                            doc_key: str = "", ip: str = "") -> int:
        """恢复某 (模块,目标,类型) 下所有生效中的忽略（全局 + 该文档范围）。"""
        cur = self._exec_retry(
            "UPDATE ignores SET restored_at=?, restored_ip=?"
            " WHERE module_key=? AND target=? AND kind=? AND restored_at IS NULL"
            " AND (doc_key='' OR doc_key=?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip or "",
             module_key, target, kind, doc_key or ""))
        self.commit()
        return int(cur.rowcount) if cur is not None else 0

    def list_ignores(self, module_key: str = None, active_only: bool = True) -> list[dict]:
        """忽略记录（默认只看生效中的）；按 id 倒序。"""
        sql = ("SELECT id, module_key, target, doc_key, kind, reason, created_at,"
               " created_ip, restored_at, restored_ip FROM ignores")
        where, params = [], []
        if module_key:
            where.append("module_key=?")
            params.append(module_key)
        if active_only:
            where.append("restored_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        cols = ["id", "module_key", "target", "doc_key", "kind", "reason", "created_at",
                "created_ip", "restored_at", "restored_ip"]
        return [dict(zip(cols, r)) for r in self._conn.execute(sql, params).fetchall()]

    def active_ignore_map(self) -> dict:
        """生效中的忽略按模块归组 → {module_key: [{target,doc_key,kind}]}（匹配用）。"""
        out: dict = {}
        for r in self.list_ignores(active_only=True):
            out.setdefault(r["module_key"], []).append(
                {"target": r["target"], "doc_key": r["doc_key"], "kind": r["kind"]})
        return out

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

    # ---- 写操作通用：自动重试 'database is locked' ----
    def _exec_retry(self, sql: str, params: tuple = (), tries: int = 30):
        """执行写语句，遇 'database is locked' 自动等待重试。

        其他进程（如长跑的全量链接检查）持有写事务时，仅靠 busy_timeout 可能超时；
        这里最多重试 tries 次（每次 2s），跨进程争用也能自愈。
        """
        for i in range(tries):
            try:
                return self._conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or i == tries - 1:
                    raise
                time.sleep(2)

    # ---- runs（通用任务历史）----
    def start_run(self, module_key: str) -> int:
        cur = self._exec_retry(
            "INSERT INTO runs (module_key, started_at, status) VALUES (?,?, 'running')",
            (module_key, datetime.now().isoformat(timespec="seconds")),
        )
        self.commit()
        assert cur is not None
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict, status: str = "success",
                   error: str = None):
        self._exec_retry(
            "UPDATE runs SET finished_at=?, status=?, summary_json=?, error=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), status,
             json.dumps(summary, ensure_ascii=False), error, run_id),
        )
        self.commit()

    def add_item(self, run_id: int, item_key: str, item_type: str, detail: dict):
        self._exec_retry(
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

    def commit(self, tries: int = 30):
        """带重试的提交（写锁被其他进程占用时等待，而不是直接失败）。"""
        for i in range(tries):
            try:
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or i == tries - 1:
                    raise
                time.sleep(2)

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

    # ---- 链接健康检查 ----
    def get_url_cache(self, url: str) -> str | None:
        r = self._conn.execute(
            "SELECT status FROM url_cache WHERE url=?", (url,)).fetchone()
        return r[0] if r else None

    def get_url_cache_entry(self, url: str) -> tuple[str, str] | None:
        """返回 (status, updated_at) 或 None（供 TTL 判断）。"""
        r = self._conn.execute(
            "SELECT status, updated_at FROM url_cache WHERE url=?", (url,)).fetchone()
        return (r[0], r[1]) if r else None

    def set_url_cache(self, url: str, status: str):
        # 不即时 commit：批量写入后由调用方统一 commit，减少锁竞争
        self._exec_retry(
            "INSERT INTO url_cache (url, status, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(url) DO UPDATE SET status=excluded.status,"
            " updated_at=excluded.updated_at",
            (url, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

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

    # ---- 邮件订阅 ----
    def add_subscriber(self, email: str) -> bool:
        """新增或重新激活订阅。返回 True=新增, False=已存在。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self._conn.execute(
            "SELECT 1 FROM subscribers WHERE email=? AND status='active'",
            (email,))
        if cur.fetchone():
            return False
        self._conn.execute(
            "INSERT INTO subscribers (email, created_at, status) VALUES (?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET status='active', created_at=?",
            (email, now, "active", now))
        self._conn.commit()
        return True

    def remove_subscriber(self, email: str) -> bool:
        cur = self._conn.execute(
            "UPDATE subscribers SET status='unsubscribed' WHERE email=?",
            (email,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_active_subscribers(self) -> list[str]:
        cur = self._conn.execute(
            "SELECT email FROM subscribers WHERE status='active' ORDER BY created_at")
        return [r[0] for r in cur.fetchall()]

    def count_subscribers(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE status='active'").fetchone()[0]

    # ---- 用户（GitCode OAuth 登录）----
    _USER_COLS = ("id", "gitcode_id", "login", "name", "avatar_url", "email",
                  "created_at", "last_login_at")

    def _row_to_user(self, row) -> dict | None:
        return dict(zip(self._USER_COLS, row)) if row else None

    def upsert_user(self, profile: dict) -> dict | None:
        """按 gitcode_id 插入或更新登录用户，返回库中记录（dict）。

        created_at 仅首次写入；每次登录刷新 login/name/avatar_url/email 与 last_login_at。
        """
        gid = str(profile.get("gitcode_id") or "").strip()
        if not gid:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._exec_retry(
            "INSERT INTO users (gitcode_id, login, name, avatar_url, email,"
            " created_at, last_login_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(gitcode_id) DO UPDATE SET"
            " login=excluded.login, name=excluded.name,"
            " avatar_url=excluded.avatar_url, email=excluded.email,"
            " last_login_at=excluded.last_login_at",
            (gid, profile.get("login") or "", profile.get("name") or "",
             profile.get("avatar_url") or "", profile.get("email") or "",
             now, now))
        self.commit()
        return self.get_user(gid)

    def get_user(self, gitcode_id: str) -> dict | None:
        """按 gitcode_id 取用户；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT id, gitcode_id, login, name, avatar_url, email, created_at,"
            " last_login_at FROM users WHERE gitcode_id=?",
            (str(gitcode_id or ""),)).fetchone()
        return self._row_to_user(row)

    def get_user_by_id(self, user_id: int) -> dict | None:
        """按本地自增 id 取用户；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT id, gitcode_id, login, name, avatar_url, email, created_at,"
            " last_login_at FROM users WHERE id=?",
            (user_id,)).fetchone()
        return self._row_to_user(row)

    def count_users(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
