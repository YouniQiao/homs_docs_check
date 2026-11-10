#!/usr/bin/env python3
"""AI 复核：图片 OCR 检查

取「英文文档里检出中文」的问题条目，让 AI 结合截图判断：
  real           真要整改（本应英文却为中文）
  false_positive 误报/正常（代码示例中的中文、OCR 误识别、正常术语等）
  uncertain      存疑

两种用法：
  1) 命令行（排查/试点）
       python3 scripts/ai_review_ocr.py --limit 20 --dry-run
       python3 scripts/ai_review_ocr.py --run 394 --only-missing
  2) 定时任务里当函数调（ocr_check.py 增量跑完 / recheck.py 复查跑完）
       import ai_review_ocr
       ai_review_ocr.run_for(run_id, limit=500, only_missing=True)

成本控制：只处理 lang=en 的 has_cn 条目（中文文档里的中文是正常的，不送判定）
          + 结果进 ai_cache（内容未变不重复调用）
          + only_missing（已判定过的不再判定）
          + --limit。
fail-soft：单条失败只记日志，不影响整体；AI 未配置时直接跳过。
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


def collect(db: IndexDB, run_id: int | None = None, limit: int = 0,
            lang: str = "en", only_missing: bool = False) -> tuple[int, list[dict]]:
    """取 has_cn 问题条目；默认取「含 has_cn 的最大 run」。

    lang: 只看该语言的图（"en"=英文文档里的中文，默认；""/"all"=不限）
    only_missing: 跳过 ai_notes 里已判定过的条目
    """
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

    judged: set[str] = set()
    if only_missing:
        judged = set(ai.note_get("ocr", RUBRIC).keys())

    items, seen = [], set()
    for ikey, dj in rows:
        try:
            d = json.loads(dj or "{}")
        except Exception:
            continue
        if lang and lang != "all" and d.get("lang") != lang:
            continue
        img = d.get("image") or ikey
        if img in seen:          # 同一张图去重
            continue
        if only_missing and ikey in judged:
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


def run_for(run_id: int | None = None, *, limit: int = 0, workers: int = 4,
            tier: str = "fast", dry: bool = False, lang: str = "en",
            only_missing: bool = False, quiet: bool = False) -> dict:
    """对某个 run 做 AI 复核。供定时任务调用；不抛异常（配置缺失时返回 skipped）。"""
    from collections import Counter

    cfg = ai.load_config()
    if not cfg.configured:
        return {"skipped": "AI 未配置（缺 .ai.env）", "run_id": run_id, "total": 0}
    t0 = time.time()
    try:
        ai.ensure_schema()
        db = IndexDB(str(ROOT / "index.db"))
        try:
            run_id, items = collect(db, run_id, limit, lang, only_missing)
        finally:
            db.close()
    except Exception as ex:  # noqa: BLE001
        return {"skipped": f"取条目失败：{ex}", "run_id": run_id, "total": 0}

    if not items:
        return {"run_id": run_id, "total": 0, "counts": {}, "cached": 0,
                "errors": 0, "elapsed": 0}

    if not quiet:
        print(f"   [AI] run#{run_id} 待判定 {len(items)} 条（模型 {cfg.model(tier)}"
              f"{'，已判过的跳过' if only_missing else ''}）", flush=True)

    out: list[tuple[str, dict, str | None]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(review_one, it, tier, dry) for it in items]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            out.append(r)
            if not quiet:
                print(f"     [{i}/{len(items)}] {(r[1].get('verdict') or '-'):<14} "
                      f"{pathlib.Path(r[0]).name[:40]}  {r[1].get('reason','')[:40]}", flush=True)

    cnt = Counter(r[1].get("verdict") for r in out)
    cached = sum(1 for r in out if (r[1].get("_meta") or {}).get("cached"))
    errs = [r for r in out if r[2]]
    res = {"run_id": run_id, "total": len(out), "counts": dict(cnt),
           "cached": cached, "errors": len(errs), "elapsed": round(time.time() - t0)}
    if not quiet:
        print(f"   [AI] 判定 {len(out)} 条 | "
              + " · ".join(f"{k} {v}" for k, v in cnt.most_common())
              + f" | 缓存命中 {cached} | 失败 {len(errs)} | 耗时 {res['elapsed']}s", flush=True)
        if errs:
            print(f"   [AI] 失败样例：{errs[0][2][:150]}", flush=True)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=None, help="指定 run（默认含 has_cn 的最大 run）")
    ap.add_argument("--limit", type=int, default=20, help="最多处理多少条（0=不限）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tier", default="fast", choices=["fast", "smart"])
    ap.add_argument("--lang", default="en", help="只判定该语言的图（en/cn/all，默认 en）")
    ap.add_argument("--only-missing", action="store_true", help="跳过已判定过的条目")
    ap.add_argument("--dry-run", action="store_true", help="只判定不写库")
    args = ap.parse_args()

    res = run_for(args.run, limit=args.limit, workers=args.workers, tier=args.tier,
                  dry=args.dry_run, lang=args.lang, only_missing=args.only_missing)
    if res.get("skipped"):
        print(f"✗ {res['skipped']}", file=sys.stderr)
        return 2 if "未配置" in str(res["skipped"]) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
