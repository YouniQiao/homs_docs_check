#!/usr/bin/env python3
"""图片内容规范检查（module_key='imgnorm'）。

读取 OCR 已识别的图片文本（items.detail_json.ocr_text），按规则集检出规范问题，
写入 runs/items。**不重跑 OCR**，纯扫库，秒级完成。

用法：
  python3 imgnorm/img_norm.py              # 扫描全部已 OCR 图片
  python3 imgnorm/img_norm.py --dry-run    # 只统计不写库
  python3 imgnorm/img_norm.py --limit 200  # 只处理前 N 张（测试）
  python3 imgnorm/img_norm.py --top 15     # 额外打印命中 Top 图

规则见 imgnorm/rules.py。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402
from imgnorm.rules import FIELD, RULES, analyze  # noqa: E402


def load_ocr_images(db) -> dict:
    """取每张图最新一条 OCR 结果：{item_key: detail_dict}。"""
    rows = db._conn.execute(
        "SELECT i.item_key, i.detail_json FROM items i "
        "JOIN runs r ON i.run_id = r.id WHERE r.module_key='ocr' "
        "ORDER BY i.id ASC"
    ).fetchall()
    latest: dict[str, dict] = {}
    for item_key, detail_json in rows:
        try:
            latest[item_key] = json.loads(detail_json)
        except Exception:
            continue
    return latest


def main():
    ap = argparse.ArgumentParser(description="图片内容规范检查（基于已 OCR 文本）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（0=全部）")
    ap.add_argument("--top", type=int, default=8, help="打印命中明细的样例行数")
    args = ap.parse_args()

    db = IndexDB(str(BASE_DIR / "index.db"))
    images = load_ocr_images(db)
    keys = list(images)
    if args.limit:
        keys = keys[:args.limit]

    t0 = time.time()
    counts = {FIELD[r["id"]]: 0 for r in RULES}
    issues = 0
    samples: list[tuple] = []
    run_id = None if args.dry_run else db.start_run("imgnorm")

    for key in keys:
        d = images[key]
        text = d.get("ocr_text") or ""
        lang = d.get("lang") or ""
        conf = d.get("confidence") or 0.0
        hit, evidence, ev_text = analyze(text, lang, conf)
        if not hit:
            continue
        issues += 1
        for field, n in hit.items():
            counts[field] += 1
        if len(samples) < args.top:
            samples.append((key, ev_text))
        if run_id is not None:
            detail = {
                "image": d.get("image") or key,
                "doc_key": d.get("doc_key", ""),
                "lang": lang,
                "catalog": d.get("catalog", ""),
                "doc_url": d.get("doc_url", ""),
                "confidence": conf,
                "evidence_text": ev_text,
                "evidence": evidence,
                "ocr_text": text[:800],
            }
            detail.update(hit)
            db.add_item(run_id, key, "issue", detail)

    elapsed = time.time() - t0
    summary = {"checked": len(keys), "issues": issues}
    summary.update(counts)
    # 敏感信息合计（密钥/IP/手机号/邮箱任一命中）
    summary["n_sensitive"] = sum(
        1 for k in keys
        if any(analyze(images[k].get("ocr_text") or "",
                       images[k].get("lang") or "",
                       images[k].get("confidence") or 0)[0].get(FIELD[x], 0)
               for x in ("secret", "ip", "phone", "email")))
    summary["elapsed_sec"] = round(elapsed)
    if run_id is not None:
        db.finish_run(run_id, summary)
    db.close()

    print(f"{'[dry-run] ' if args.dry_run else ''}检查图片 {len(keys)} | 问题图片 {issues} | "
          f"耗时 {elapsed:.1f}s")
    for r in RULES:
        print(f"   {r['label']:14s} {counts[FIELD[r['id']]]:6d}")
    if samples:
        print("\n样例：")
        for key, ev in samples:
            print(f"   {key}\n      {ev[:140]}")


if __name__ == "__main__":
    main()
