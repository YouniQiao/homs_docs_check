#!/usr/bin/env python3
"""AI 复核：图片 OCR 检查（试点）

取「英文文档里检出中文」的问题条目，让 AI 结合截图判断：
  real           真要整改（本应英文却为中文）
  false_positive 误报/正常（代码示例中的中文、OCR 误识别、正常术语等）
  uncertain      存疑

用法：
  python3 scripts/ai_review_ocr.py --limit 20 --dry-run    # 只看不改
  python3 scripts/ai_review_ocr.py --limit 50              # 写 ai_notes

成本控制：只处理问题条目 + 结果进 ai_cache（未变不重复调用）+ --limit。
fail-soft：单条失败只记日志，不影响整体。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai  # noqa: E402
from db import IndexDB  # noqa: E402

RUBRIC = "ocr_false_positive"


def collect(db: IndexDB, run_id: int | None, limit: int) -> tuple[int, list[dict]]:
    """取 has_cn 问题条目（默认：含 has_cn 的最大 run）。"""
    if run_id is None:
        row = db._conn.execute(
            "SELECT run_id FROM items WHERE item_type='has_cn' "
            "ORDER BY run_id DESC LIMIT 1").fetchone()
        if not row:
            return 0, []
        run_id = row[0]
    rows = db._conn.execute(
        "SELECT item_key, detail_json FROM items "
        "WHERE run_id=? AND item_type='has_cn' ORDER BY item_key", (run_id,)).fetchall()
    items, seen = [], set()
    for ikey, dj in rows:
        try:
            d = json.loads(dj or "{}")
        except Exception:
            continue
        img = d.get("image") or ikey
        if img in seen:          # 同一张图去重
            continue
        seen.add(img)
        items.append({"item_key": ikey, "image": img, "detail": d})
    if limit:
        items = items[:limit]
    return run_id, items


def review_one(it: dict, tier: str, dry: bool) -> tuple[str, dict, str | None]:
    d = it["detail"]
    img_path = ROOT / it["image"]
    if not img_path.exists():
        return it["item_key"], {"verdict": "skipped", "reason": "图片不存在"}, "图片不存在"
    # 低置信度行（原文里挑出来给模型参考）
    lines = d.get("lines") or []
    try:
        lows = [f"{l.get('text','')}({l.get('confidence')})"
                for l in lines if float(l.get("confidence") or 1) < 0.9][:8]
    except Exception:
        lows = []
    fields = {"doc_url": d.get("doc_url", ""),
              "ocr_text": d.get("ocr_text", ""),
              "low_conf": "; ".join(lows) or "（无）"}
    try:
        res = ai.review_image(RUBRIC, fields, str(img_path), module="ocr",
                              item_key=it["item_key"], tier=tier)
    except Exception as ex:  # noqa: BLE001
        return it["item_key"], {"verdict": "error", "reason": str(ex)[:120]}, str(ex)
    if not dry:
        ai.note_put("ocr", it["item_key"], RUBRIC, res, ai.load_config().model(tier),
                    ai.prompt_version(RUBRIC))
    return it["item_key"], res, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=None, help="指定 run（默认含 has_cn 的最大 run）")
    ap.add_argument("--limit", type=int, default=20, help="最多处理多少条（0=不限）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tier", default="fast", choices=["fast", "smart"])
    ap.add_argument("--dry-run", action="store_true", help="只判定不写库")
    args = ap.parse_args()

    cfg = ai.load_config()
    if not cfg.configured:
        print("✗ AI 未配置（缺 .ai.env）", file=sys.stderr)
        return 2
    ai.ensure_schema()

    db = IndexDB(str(ROOT / "index.db"))
    run_id, items = collect(db, args.run, args.limit)
    print(f"📥 run#{run_id} 问题条目（去重后）{len(items)} 条  | 模型 {cfg.model(args.tier)}  | "
          f"{'DRY-RUN（不写库）' if args.dry_run else '写入 ai_notes'}")
    if not items:
        return 0

    t0 = time.time()
    out: list[tuple[str, dict, str | None]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(review_one, it, args.tier, args.dry_run) for it in items]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            out.append(r)
            v = r[1].get("verdict")
            print(f"  [{i}/{len(items)}] {v:<14} {pathlib.Path(r[0]).name[:40]}  {r[1].get('reason','')[:40]}")

    # 汇总
    from collections import Counter
    cnt = Counter(r[1].get("verdict") for r in out)
    cached = sum(1 for r in out if (r[1].get("_meta") or {}).get("cached"))
    errs = [r for r in out if r[2]]
    print(f"\n📊 判定 {len(out)} 条 | " + " · ".join(f"{k} {v}" for k, v in cnt.most_common()))
    print(f"   缓存命中 {cached} | 失败 {len(errs)} | 耗时 {time.time()-t0:.0f}s")
    if errs:
        print("   失败样例:", errs[0][2][:150])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
