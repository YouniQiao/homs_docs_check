#!/usr/bin/python3
"""把回扫结果导入数据库：写 render_cache，并把 dead 的链接标成 PSEUDO_DEAD。

只做一次性小事务（几秒），避免和夜间定时任务抢写锁。

用法：import_render.py [--in linkcheck/pseudo_backfill.jsonl] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "index.db"
DEFAULT_IN = Path(__file__).resolve().parent / "pseudo_backfill.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(DEFAULT_IN))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"找不到结果文件：{src}")
        return
    rows: dict[str, str] = {}
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows[r["url"]] = r["verdict"]          # 同 URL 取最后一次

    conn = sqlite3.connect(f"file:{DB}", uri=True, timeout=180)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=180000")
    conn.execute("CREATE TABLE IF NOT EXISTS render_cache "
                 "(url TEXT PRIMARY KEY, verdict TEXT, checked_at TEXT)")

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    dead = [u for u, v in rows.items() if v == "dead"]
    ok = [u for u, v in rows.items() if v == "ok"]
    unk = [u for u, v in rows.items() if v == "unknown"]
    print(f"待导入 {len(rows)} 条：dead {len(dead)} / ok {len(ok)} / unknown {len(unk)}")
    if args.dry_run:
        print("[dry-run] 不写库")
        return

    t0 = time.time()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO render_cache(url, verdict, checked_at) VALUES (?,?,?)",
            [(u, v, stamp) for u, v in rows.items()])
        if dead:
            conn.executemany(
                "UPDATE url_cache SET status = 'PSEUDO_DEAD' WHERE url = ?",
                [(u,) for u in dead])
    conn.close()
    print(f"导入完成：{len(rows)} 条，伪200 {len(dead)} 条，用时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
