#!/usr/bin/env python3
"""OCR 检查任务：扫描文档图片，识别文字，结果写入 runs/items（module_key='ocr'）。

用法：
  python3 ocr_check.py                  # 增量：基于 sync 最新变更，只跑新增/修改文档的图片
  python3 ocr_check.py --full           # 全量：扫描所有文档图片
  python3 ocr_check.py --workers 12     # 并发进程数（默认 12，16 核建议 12）
  python3 ocr_check.py --dry-run        # 只统计本次要跑的图片数

执行策略：
  全量   —— 遍历 data/ 下所有 .md 提取图片引用
  增量   —— 读 sync 最新 run 的 added/modified 文档，只跑这些文档的图片
  去重   —— item_key = 图片相对路径，已检查过的跳过（除非 --force）
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402

IMG_RE = re.compile(r"!\[[^\]]*\]\(images/([^)]+)\)")
DATA_DIR = BASE_DIR / "data"


def collect_images(doc_keys: set[str] | None = None) -> list[tuple]:
    """收集图片任务列表 [(img_rel, doc_key, lang, catalog), ...]。

    doc_keys=None 全量：所有 md 引用的图片。
    doc_keys=set 增量：只收集这些文档（doc_key）md 里引用的图片。
    孤儿图（未被任何 md 引用）不检查。
    """
    tasks = []
    for lang_dir in sorted(DATA_DIR.iterdir()):
        if not lang_dir.is_dir() or lang_dir.name.startswith("."):
            continue
        lang = lang_dir.name
        for cat_dir in sorted(lang_dir.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            catalog = cat_dir.name
            images_dir = cat_dir / "images"
            for md in sorted(cat_dir.glob("*.md")):
                doc_key = f"{lang}|{catalog}|{md.stem}"
                if doc_keys is not None and doc_key not in doc_keys:
                    continue
                try:
                    content = md.read_text(encoding="utf-8")
                except Exception:
                    continue
                for m in IMG_RE.finditer(content):
                    fname = m.group(1)
                    if not (images_dir / fname).exists():
                        continue
                    img_rel = f"data/{lang}/{catalog}/images/{fname}"
                    tasks.append((img_rel, doc_key, lang, catalog))
    return tasks


def _worker(batch: list[tuple]) -> list[tuple]:
    """进程内批量跑 OCR（每进程初始化一次引擎，模型常驻）。"""
    from ocr.ocr_engine import PaddleOcrEngine

    engine = PaddleOcrEngine()
    results = []
    for img_rel, doc_key, lang, catalog in batch:
        img_path = BASE_DIR / img_rel
        try:
            res = engine.recognize(str(img_path))
            results.append((img_rel, doc_key, lang, catalog, res))
        except Exception as e:
            results.append((img_rel, doc_key, lang, catalog, {"error": str(e)}))
    return results


def main():
    parser = argparse.ArgumentParser(description="OCR 图片检查（识别文字）")
    parser.add_argument("--full", action="store_true", help="全量扫描所有图片")
    parser.add_argument("--workers", type=int, default=12, help="并发进程数")
    parser.add_argument("--batch-size", type=int, default=16, help="每批图片数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不执行")
    parser.add_argument("--force", action="store_true", help="忽略去重，强制重跑")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制本次处理张数（测试/分批用，0=不限）")
    args = parser.parse_args()

    db = IndexDB(str(BASE_DIR / "index.db"))

    # 1. 确定要跑的图片集合
    if args.full:
        tasks = collect_images()
        print(f"📦 全量模式：扫描全部文档图片", flush=True)
    else:
        # 增量：取最近一次已完成的 sync run 的 added/modified（全部语言）
        runs = db.list_runs("sync", limit=5)
        run = next((r for r in runs if r["status"] == "success"), None)
        doc_keys: set[str] = set()
        if run:
            for it in db.get_items(run["id"]):
                if it["item_type"] in ("added", "modified"):
                    doc_keys.add(it["item_key"])
        tasks = collect_images(doc_keys)
        print(f"📦 增量模式：sync run#{run['id'] if run else None} 的变更文档 "
              f"（{len(doc_keys)} 篇），提取图片", flush=True)

    # 2. 去重：同一张图只跑一次（保留首次引用），并跳过已检查的
    if args.force:
        todo = tasks
    else:
        checked = {r[0] for r in db._conn.execute(
            "SELECT DISTINCT i.item_key FROM items i "
            "JOIN runs r ON i.run_id = r.id WHERE r.module_key='ocr'")}
    seen: set[str] = set()
    todo = []
    for t in tasks:
        if t[0] in seen:
            continue
        seen.add(t[0])
        if not args.force and t[0] in checked:
            continue
        todo.append(t)

    print(f"   收集 {len(tasks)} 张（含重复引用），唯一图片 {len(seen)}，"
          f"本次待跑 {len(todo)}", flush=True)

    if args.dry_run:
        db.close()
        print("   [dry-run] 结束", flush=True)
        return

    if not todo:
        # 无可跑图片：仍记录一条空 run，便于确认定时任务已执行
        run_id = db.start_run("ocr")
        summary = {"total": 0, "has_cn": 0, "no_cn": 0, "en_has_cn": 0,
                   "errors": 0, "elapsed_sec": 0, "rate_per_sec": 0}
        db.finish_run(run_id, summary)
        db.close()
        print("   无可跑图片，已记录空 run 确认执行", flush=True)
        return

    if args.limit:
        todo = todo[:args.limit]
        print(f"   --limit {args.limit}，实际跑 {len(todo)} 张", flush=True)

    # 3. 多进程跑 OCR
    run_id = db.start_run("ocr")
    t0 = time.time()
    batches = [todo[i:i + args.batch_size]
               for i in range(0, len(todo), args.batch_size)]
    summary = {"total": 0, "has_cn": 0, "no_cn": 0, "en_has_cn": 0, "errors": 0}
    done_batches = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_worker, b): b for b in batches}
        for fut in concurrent.futures.as_completed(futures):
            for img_rel, doc_key, lang, catalog, res in fut.result():
                summary["total"] += 1
                if "error" in res:
                    summary["errors"] += 1
                    db.add_item(run_id, img_rel, "error", {
                        "image": img_rel, "doc_key": doc_key, "lang": lang,
                        "catalog": catalog, "error": res["error"],
                    })
                    continue
                item_type = "has_cn" if res["has_cn"] else "no_cn"
                if res["has_cn"]:
                    summary["has_cn"] += 1
                    if lang == "en":
                        summary["en_has_cn"] += 1
                else:
                    summary["no_cn"] += 1
                d = db.get_doc(doc_key)
                doc_url = d.get("url", "") if d else ""
                db.add_item(run_id, img_rel, item_type, {
                    "image": img_rel,
                    "doc_key": doc_key,
                    "lang": lang,
                    "catalog": catalog,
                    "doc_url": doc_url,
                    "ocr_text": res["text"],
                    "lines": res["lines"],
                    "has_cn": res["has_cn"],
                    "confidence": res["max_confidence"],
                })
            done_batches += 1
            # 每批 commit 释放写锁（WAL 下其他写者才能抢到锁；
            # 不 commit 会独占写事务数小时，阻塞 encheck/sync 等）
            db.commit()
            if done_batches % 10 == 0 or done_batches == len(futures):
                print(f"   进度: {done_batches}/{len(futures)} 批 "
                      f"({summary['total']}/{len(todo)} 张)", flush=True)

    elapsed = time.time() - t0
    summary["elapsed_sec"] = round(elapsed)
    summary["rate_per_sec"] = round(summary["total"] / elapsed, 2) if elapsed else 0
    db.finish_run(run_id, summary)
    print(f"✅ OCR 完成: {summary['total']} 张 | 含中文 {summary['has_cn']} "
          f"(英文图含中文 {summary['en_has_cn']}) | 无中文 {summary['no_cn']} | "
          f"错误 {summary['errors']} | 耗时 {elapsed / 60:.1f} 分钟 "
          f"({summary['rate_per_sec']} 张/秒)", flush=True)
    db.close()


if __name__ == "__main__":
    main()
