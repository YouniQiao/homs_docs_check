"""回填 display_update_time（增量判据）和 content_hash（兜底）两个字段。

用途：判据从 hash 切换到 displayUpdateTime 时，旧索引缺 display_update_time，
需要一次性回填。一次 API 调用同时回填两个字段。
"""
import concurrent.futures
import sys
from pathlib import Path

sys.path.insert(0, "/opt/projects/harmonyos_docs")

from db import IndexDB
from hw_api import get_document
from sync import stable_hash


def backfill_one(lang, catalog, rel):
    try:
        value = get_document(rel, catalog, lang)
        html = value.get("content", {}).get("content", "")
        if not html:
            return None
        display_time = value.get("displayUpdateTime", "")
        content_hash = stable_hash(html)
        return (display_time, content_hash)
    except Exception:
        return None


def main(workers: int = 8):
    db = IndexDB("index.db")
    conn = db._conn
    rows = conn.execute(
        "SELECT doc_key, lang, catalog, relate_document FROM docs"
    ).fetchall()
    print(f"需要回填的文档: {len(rows)}", flush=True)

    if not rows:
        db.close()
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(backfill_one, lang, catalog, rel): doc_key
            for doc_key, lang, catalog, rel in rows
        }
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            doc_key = futures[fut]
            res = fut.result()
            if res:
                display_time, content_hash = res
                conn.execute(
                    "UPDATE docs SET display_update_time=?, content_hash=? "
                    "WHERE doc_key=?",
                    (display_time, content_hash, doc_key),
                )
            done += 1
            if done % 500 == 0 or done == len(rows):
                print(f"回填进度: {done}/{len(rows)}", flush=True)

    db.commit()
    empty = conn.execute(
        "SELECT COUNT(*) FROM docs WHERE display_update_time IS NULL OR display_update_time=''"
    ).fetchone()[0]
    print(f"回填完成，剩余空 display_update_time: {empty}", flush=True)
    db.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    main(args.workers)
