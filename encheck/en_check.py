#!/usr/bin/env python3
"""英文文档检查：检测中文字符（汉字 + 中文标点）与中文跳转链接。

用法：
  python3 en_check.py                # 增量：基于 sync 最新 en 变更
  python3 en_check.py --full         # 全量：所有 en 文档（~1 万篇，纯文本检查，几分钟）
  python3 en_check.py --dry-run      # 只统计
  python3 en_check.py --force        # 忽略去重，强制重跑
  python3 en_check.py --workers 8    # 并发进程数（默认 8）

结果写 runs/items（module_key='encheck'），每篇文档一个 item：
  item_type = has_cn / has_cn_link / both / clean
  detail: doc_key, title, doc_url, catalog, cn_chars（去重字符列表）,
          cn_char_count, cn_links（[{text,url}]）, cn_link_count
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402

DATA_DIR = BASE_DIR / "data"

# 中文汉字（基本 CJK 统一表意文字）
CN_HANZI_RE = re.compile(r"[\u4e00-\u9fff]")
# 中文标点：除汉字外的中文标点（顿号句号、全角逗号句号问号叹号冒号分号、括号、书名号、引号、省略号破折号间隔号等）
CN_PUNCT_RE = re.compile(
    "[\u3001\u3002"                        # 、。
    "\uff0c\uff0e\uff01\uff1f\uff1a\uff1b"  # ，．！？：；
    "\uff08\uff09"                          # （）
    "\u3010\u3011\u300a\u300b\u300c\u300d\u300e\u300f\u3014\u3015"  # 【】《》「」『』〔〕
    "\u201c\u201d\u2018\u2019"              # "" ''
    "\u2026\u2014\u00b7\uff5e\uffe5"        # …—·～￥
    "]"
)
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
BARE_URL_RE = re.compile(r"https?://[^\s)\]]+")
# cn 作为独立段：/cn/ ?cn= =cn &cn# 等边界；排除 xxxcn.com、zh-cn 这类
CN_LINK_RE = re.compile(r"(?:^|[/?&#=])cn(?:[/?&#=]|$)", re.IGNORECASE)


def _strip_urls(content: str) -> str:
    """挖掉链接 URL（含中文锚点如 #概述）和裸 URL，保留链接文字。

    链接文字（用户可见的 [text]）里的中文仍保留检测；URL 里的中文
    （华为官网英文链接锚点用中文标题，如 #概述）不算正文中文。
    """
    content = LINK_RE.sub(lambda m: f"[{m.group(1)}](URL)", content)
    return BARE_URL_RE.sub("URL", content)


def check_md(content: str) -> tuple:
    """返回 (正文汉字去重列表, 汉字数, 正文标点去重列表, 标点数,
    URL中文去重列表, URL中文数, URL含中文的链接列表, 中文链接列表, 中文链接数)。

    正文汉字/标点：md 正文（已挖掉 URL）里的中文，汉字与标点分开统计。
    URL 中文：链接 URL 里的中文（如官网英文链接锚点 #概述）——也是本地化问题。
    """
    cleaned = _strip_urls(content)
    hanzi = CN_HANZI_RE.findall(cleaned)
    hz_unique = sorted(set(hanzi))
    punct = CN_PUNCT_RE.findall(cleaned)
    pt_unique = sorted(set(punct))
    url_chars: list[str] = []
    url_cn_links: list[dict] = []
    cn_links: list[dict] = []
    for m in LINK_RE.finditer(content):
        text, url = m.group(1).strip(), m.group(2).strip()
        if CN_HANZI_RE.search(url) or CN_PUNCT_RE.search(url):
            url_chars.extend(CN_HANZI_RE.findall(url))
            url_chars.extend(CN_PUNCT_RE.findall(url))
            url_cn_links.append({"text": text[:60], "url": url})
        if CN_LINK_RE.search(url):
            cn_links.append({"text": text[:60], "url": url})
    for m in BARE_URL_RE.finditer(content):
        url_chars.extend(CN_HANZI_RE.findall(m.group(0)))
        url_chars.extend(CN_PUNCT_RE.findall(m.group(0)))
    url_unique = sorted(set(url_chars))
    return (hz_unique, len(hanzi), pt_unique, len(punct),
            url_unique, len(url_chars), url_cn_links, cn_links, len(cn_links))


def collect_en_docs(doc_keys: set[str] | None = None) -> list[tuple]:
    """收集 en 文档 [(doc_key, md_path), ...]。doc_keys=None 全量。"""
    docs = []
    en_dir = DATA_DIR / "en"
    for cat_dir in sorted(en_dir.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith("."):
            continue
        catalog = cat_dir.name
        for md in sorted(cat_dir.glob("*.md")):
            doc_key = f"en|{catalog}|{md.stem}"
            if doc_keys is not None and doc_key not in doc_keys:
                continue
            docs.append((doc_key, str(md)))
    return docs


def _worker(batch: list[tuple]) -> list[tuple]:
    """进程内批量检查（纯文本 + 正则，无模型）。"""
    results = []
    for doc_key, md_path in batch:
        try:
            content = Path(md_path).read_text(encoding="utf-8")
            hz, hc, pt, pc, uu, cu, url_links, links, lc = check_md(content)
            results.append((doc_key, hz, hc, pt, pc, uu, cu,
                            url_links, links, lc, None))
        except Exception as e:
            results.append((doc_key, [], 0, [], 0, [], 0, [], [], 0, str(e)))
    return results


def _write_with_retry(db, fn, *args, attempts=15, **kwargs):
    """带重试的写库操作（OCR 全量进程持续写库时，写锁竞争激烈）。

    每次失败等待 5 + i*3 秒后重试，最多 attempts 次。
    """
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
            wait = 5 + i * 3
            print(f"   ⚠️ 写锁冲突，等待 {wait}s 重试...", flush=True)
            time.sleep(wait)
    raise RuntimeError("写库失败：持续锁冲突")


def main():
    parser = argparse.ArgumentParser(description="英文文档检查（中文字符 + 中文链接）")
    parser.add_argument("--full", action="store_true", help="全量检查所有 en 文档")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略去重重跑")
    args = parser.parse_args()

    db = IndexDB(str(BASE_DIR / "index.db"))

    # 1. 确定文档集合
    if args.full:
        docs = collect_en_docs()
        print(f"📦 全量模式：扫描全部英文文档", flush=True)
    else:
        # 增量：取最近一次已完成的 sync run 的 added/modified（en 文档）
        runs = db.list_runs("sync", limit=5)
        run = next((r for r in runs if r["status"] == "success"), None)
        doc_keys: set[str] = set()
        if run:
            for it in db.get_items(run["id"]):
                if it["item_type"] in ("added", "modified") and it["item_key"].startswith("en|"):
                    doc_keys.add(it["item_key"])
        docs = collect_en_docs(doc_keys)
        print(f"📦 增量模式：sync run#{run['id'] if run else None} 的 en 变更文档 "
              f"（{len(doc_keys)} 篇）", flush=True)

    # 2. 去重（已检查的文档跳过）
    if args.force:
        todo = docs
    else:
        checked = {r[0] for r in db._conn.execute(
            "SELECT DISTINCT i.item_key FROM items i "
            "JOIN runs r ON i.run_id = r.id WHERE r.module_key='encheck'")}
        todo = [d for d in docs if d[0] not in checked]
    print(f"   收集 {len(docs)} 篇，已检查 {len(docs) - len(todo)}，本次待跑 {len(todo)}",
          flush=True)

    if args.dry_run or not todo:
        db.close()
        print("   [dry-run 或无可检查文档] 结束", flush=True)
        return

    # 3. 多进程检查（结果先攒内存，检查完再集中写库，减少写锁竞争）
    t0 = time.time()
    batches = [todo[i:i + args.batch_size]
               for i in range(0, len(todo), args.batch_size)]
    summary = {"total": 0, "hanzi": 0, "punct": 0, "url_cn": 0, "cn_link": 0,
               "clean": 0, "errors": 0}
    results: list[tuple] = []
    done = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_worker, b) for b in batches]
        for fut in concurrent.futures.as_completed(futures):
            for r in fut.result():
                doc_key, hz, hc, pt, pc, uu, cu, url_links, links, lc, err = r
                summary["total"] += 1
                if err:
                    summary["errors"] += 1
                else:
                    if hc > 0:
                        summary["hanzi"] += 1
                    if pc > 0:
                        summary["punct"] += 1
                    if cu > 0:
                        summary["url_cn"] += 1
                    if lc > 0:
                        summary["cn_link"] += 1
                    if not (hc or pc or cu or lc):
                        summary["clean"] += 1
                results.append(r)
            done += 1
            if done % 10 == 0 or done == len(futures):
                print(f"   进度: {done}/{len(futures)} 批 "
                      f"({summary['total']}/{len(todo)} 篇)", flush=True)

    # 4. 集中写库（带写锁重试；OCR 全量进程可能正持续写库）
    print("   💾 写入结果...", flush=True)
    run_id = _write_with_retry(db, db.start_run, "encheck")
    for i in range(0, len(results), 200):
        batch = results[i:i + 200]

        def _flush(batch=batch):
            for doc_key, hz, hc, pt, pc, uu, cu, url_links, links, lc, err in batch:
                if err:
                    db.add_item(run_id, doc_key, "error", {
                        "doc_key": doc_key, "lang": "en", "error": err,
                    })
                    continue
                item_type = "problem" if (hc or pc or cu or lc) else "clean"
                if item_type == "clean":
                    continue  # 不记录/不显示正常文档（summary 仍统计数量）
                d = db.get_doc(doc_key)
                db.add_item(run_id, doc_key, item_type, {
                    "doc_key": doc_key,
                    "lang": "en",
                    "catalog": doc_key.split("|")[1],
                    "title": d.get("title", "") if d else "",
                    "doc_url": d.get("url", "") if d else "",
                    "hanzi": hz,             # 正文汉字（去重）
                    "hanzi_count": hc,       # 正文汉字总数
                    "punct": pt,             # 正文标点（去重）
                    "punct_count": pc,       # 正文标点总数
                    "url_cn_chars": uu,      # 链接 URL 中文字符（去重）
                    "url_cn_char_count": cu,  # URL 中文总数
                    "url_cn_links": url_links,  # URL 含中文的链接（定位用）
                    "cn_links": links,
                    "cn_link_count": lc,
                })
            db.commit()

        _write_with_retry(db, _flush)

    elapsed = time.time() - t0
    summary["elapsed_sec"] = round(elapsed)
    _write_with_retry(db, db.finish_run, run_id, summary)
    print(f"✅ 检查完成: {summary['total']} 篇 | 含汉字 {summary['hanzi']} | "
          f"含标点 {summary['punct']} | 链接URL含中文 {summary['url_cn']} | "
          f"含中文链接 {summary['cn_link']} | 正常 {summary['clean']} | "
          f"错误 {summary['errors']} | 耗时 {elapsed:.0f} 秒", flush=True)
    db.close()


if __name__ == "__main__":
    main()
