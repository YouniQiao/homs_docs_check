#!/usr/bin/python3
"""存量伪 200 回扫（只写文件，不碰数据库；到点自动收工，可断点续跑）。

候选：url_cache 里 status=200 的站内文档链接（/doc/），排除已判为 PSEUDO_DEAD 的。
输出：linkcheck/pseudo_backfill.jsonl（每行 {"url","verdict","sec"}），已跑过的自动跳过。

用法：
  backfill_pseudo.py [--workers 8] [--timeout 25] [--stop-at 23:30] [--limit 0] [--out 路径]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cdp_render  # noqa: E402

DB = BASE / "index.db"
DEFAULT_OUT = Path(__file__).resolve().parent / "pseudo_backfill.jsonl"


def candidates() -> list[str]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT url FROM url_cache "
        "WHERE status = 200 AND url LIKE 'https://developer.huawei.com/consumer/%/doc/%'"
    ).fetchall()
    conn.close()
    return sorted({r[0] for r in rows})


def done_urls(path: Path) -> set[str]:
    """已出结论的（ok/dead）才算完成；unknown 留给下一次重试。"""
    if not path.exists():
        return set()
    out: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("verdict") in ("ok", "dead"):
                out.add(r["url"])
    return out


def past_stop(stop_at: str | None) -> bool:
    if not stop_at:
        return False
    hh, _, mm = stop_at.partition(":")
    now = datetime.now()
    return (now.hour, now.minute) >= (int(hh), int(mm or 0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--stop-at", default="23:30", help="到这个时间就收工（如 23:30），留空则不停")
    ap.add_argument("--limit", type=int, default=0, help="本次最多跑多少条（0=不限）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    out_path = Path(args.out)
    todo_all = candidates()
    seen = done_urls(out_path)
    todo = [u for u in todo_all if u not in seen]
    print(f"候选 {len(todo_all)} 条，已完成 {len(seen)} 条，本次待跑 {len(todo)} 条", flush=True)
    if args.limit:
        todo = todo[: args.limit]
        print(f"  受 --limit 限制，本次只跑 {len(todo)} 条", flush=True)
    if not todo:
        print("没有待跑的，结束。", flush=True)
        return

    t0 = time.time()
    total = len(todo)
    n = 0
    fh = open(out_path, "a", encoding="utf-8")
    CHUNK = 400
    try:
        for start in range(0, total, CHUNK):
            if past_stop(args.stop_at):
                print(f"⏰ 到 {args.stop_at} 了，收工。本次完成 {n}/{total}，剩余下次继续。",
                      flush=True)
                break
            chunk = todo[start:start + CHUNK]
            print(f"—— 批次 {start + 1}-{start + len(chunk)} / {total}（"
                  f"{args.workers} 并发，超时 {args.timeout:g}s）", flush=True)

            def prog(r: dict) -> None:
                nonlocal n
                n += 1
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                if n % 50 == 0:
                    el = time.time() - t0
                    print(f"   进度 {n}/{total}  {r['verdict']}  "
                          f"({el/60:.1f} 分钟, {el/n:.1f}s/条)", flush=True)

            cdp_render.render_batch(chunk, workers=args.workers,
                                    timeout=args.timeout, progress=prog)
    finally:
        fh.close()
    el = time.time() - t0
    print(f"本次结束：{n} 条，用时 {el/60:.1f} 分钟。"
          f"累计已完成 {len(done_urls(out_path))} 条。", flush=True)


if __name__ == "__main__":
    main()
