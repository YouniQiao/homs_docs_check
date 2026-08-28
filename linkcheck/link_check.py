"""链接健康检查。

检查文档 markdown 中的链接质量问题，每篇文档一个 item：
  - int_missing（站内未覆盖）：链接是 developer.huawei.com 站内链接，
    但指向的页面不在本地已同步的 docs 集合里（可能新文档、未同步 catalog、
    老版本 V2/V5 等）——提示本地同步覆盖缺口.
  - ext_dead（外链死链）：非华为外部链接 HTTP 状态 >= 400（404/410/5xx 等）。
    需联网；--no-http 可跳过只做站内检查。

用法：
  python3 link_check.py            # 增量（读 sync 最新 run 的变更文档）
  python3 link_check.py --full     # 全量扫描所有文档
  python3 link_check.py --dry-run  # 只统计不写库
  python3 link_check.py --no-http  # 只做站内覆盖检查，不联网
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
HUAWEI_DOC = "https://developer.huawei.com/consumer/"
# 华为生态域名，不算外部链接
HUAWEI_HOSTS = ("developer.huawei.com", "contentcenter-videovali-drcn.dbankcdn.cn",
                "contentcenter-vali-drcn.dbankcdn.cn", "ohpm.openharmony.cn",
                "petalpay-merchant.cloud.huawei.com")

UA = "Mozilla/5.0 (HarmonyOS/DocChecker)"


def _normalize(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/")


def _http_status(url: str, timeout: int = 10) -> str:
    """HEAD 优先，退回 GET；返回 '200' / '404' / 'ERR:xxx'。"""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": UA,
                                                  "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return str(r.status)
        except urllib.error.HTTPError as e:
            return str(e.code)
        except Exception as e:
            if method == "GET":
                return f"ERR:{type(e).__name__}"
    return "ERR:unknown"


def _external_links(content: str) -> list[tuple[str, str]]:
    """非华为外链 [(链接文字, url), ...]"""
    out = []
    for text, url in LINK_RE.findall(content):
        if not url.startswith(("http://", "https://")):
            continue
        if url.startswith(HUAWEI_DOC) or any(h in url for h in HUAWEI_HOSTS):
            continue
        out.append((text, url))
    return out


def _int_links(content: str, doc_urls: set[str]) -> list[tuple[str, str]]:
    """站内华为链接但本地未覆盖的 [(链接文字, url), ...]"""
    out = []
    for text, url in LINK_RE.findall(content):
        if not url.startswith(HUAWEI_DOC):
            continue
        if _normalize(url) not in doc_urls:
            out.append((text, url))
    return out


def collect_docs(doc_keys: set[str] | None = None) -> list[tuple]:
    """[(doc_key, lang, catalog, doc_url, content), ...]，支持增量过滤。"""
    docs = []
    for lang_dir in sorted((BASE_DIR / "data").iterdir()):
        if not lang_dir.is_dir() or lang_dir.name.startswith("."):
            continue
        lang = lang_dir.name
        for cat_dir in sorted(lang_dir.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            catalog = cat_dir.name
            for md in sorted(cat_dir.glob("*.md")):
                doc_key = f"{lang}|{catalog}|{md.stem}"
                if doc_keys is not None and doc_key not in doc_keys:
                    continue
                try:
                    content = md.read_text(encoding="utf-8")
                except Exception:
                    continue
                # doc_url 从 docs 表查（在 main 里赋）；这里先放空，由 main 补
                docs.append((doc_key, lang, catalog, content))
    return docs


def check_doc(doc_key: str, lang: str, catalog: str, content: str,
              doc_urls: set[str]) -> dict:
    """返回某文档的问题明细（无问题返回 None）。"""
    int_missing = _int_links(content, doc_urls)
    ext = _external_links(content)
    d = {"doc_key": doc_key, "lang": lang, "catalog": catalog,
         "int_links": [{"text": t, "url": u} for t, u in int_missing],
         "int_missing_count": len(int_missing),
         "ext_links": [], "ext_dead_count": 0,
         "ext_unknown_count": 0}
    if ext:
        d["ext_links_raw"] = ext  # [(text,url)] 待 HTTP 检查后填充
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="链接健康检查")
    ap.add_argument("--full", action="store_true", help="全量扫描")
    ap.add_argument("--dry-run", action="store_true", help="只统计")
    ap.add_argument("--no-http", action="store_true", help="跳过外链 HTTP 检查")
    ap.add_argument("--workers", type=int, default=16, help="HTTP 并发")
    ap.add_argument("--timeout", type=int, default=10, help="HTTP 超时(秒)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理文档数(调试)")
    args = ap.parse_args()

    db = IndexDB(BASE_DIR / "index.db")
    doc_urls = {r for r, in db._conn.execute("SELECT url FROM docs") if r}

    # 1. 收集要检查的文档
    if args.full:
        docs = collect_docs()
        print(f"📦 全量：扫描全部文档", flush=True)
    else:
        runs = db.list_runs("sync", limit=5)
        run = next((r for r in runs if r["status"] == "success"), None)
        keys = set()
        if run:
            for it in db.get_items(run["id"]):
                if it["item_type"] in ("added", "modified"):
                    keys.add(it["item_key"])
        docs = collect_docs(keys)
        print(f"📦 增量：sync run#{run['id'] if run else None} 变更 "
              f"{len(keys)} 篇", flush=True)
    if args.limit:
        docs = docs[:args.limit]

    print(f"   待检查 {len(docs)} 篇", flush=True)

    # 2. 站内覆盖检查（本地，快）
    problems: list[dict] = []
    ext_urls: set[str] = set()
    for doc_key, lang, catalog, content in docs:
        r = check_doc(doc_key, lang, catalog, content, doc_urls)
        if not r:
            continue
        for t, u in r.get("ext_links_raw", []):
            ext_urls.add(u)
        problems.append(r)

    # 3. 外链 HTTP 检查（可选）
    ext_status: dict[str, str] = {}
    if not args.no_http and problems:
        unique = sorted(ext_urls)
        print(f"   外链 HTTP 检查 {len(unique)} 个（并发 {args.workers}）",
              flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_http_status, u, args.timeout): u for u in unique}
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                try:
                    ext_status[u] = fut.result()
                except Exception:
                    ext_status[u] = "ERR:exec"

    # 4. 落地问题详情 + 汇总
    summary = {"total": 0, "int_missing": 0, "ext_dead": 0, "ext_unknown": 0}
    for r in problems:
        # 外链死链：非华为外链状态 >=400；ERR 记 unknown 不视为死链
        dead, unk = [], 0
        for t, u in r.get("ext_links_raw", []):
            st = ext_status.get(u, "?")
            if st.startswith("ERR"):
                unk += 1
                continue
            if st.isdigit() and int(st) >= 400:
                dead.append({"text": t, "url": u, "status": st})
        r.setdefault("ext_links", []).extend(dead)
        r["ext_dead_count"] = len(dead)
        r["ext_unknown_count"] = unk
        r.pop("ext_links_raw", None)
        if not (r["int_missing_count"] or r["ext_dead_count"]):
            continue  # 无问题不记录
        summary["total"] += 1
        if r["int_missing_count"]:
            summary["int_missing"] += 1
        if r["ext_dead_count"]:
            summary["ext_dead"] += 1
        if r["ext_unknown_count"]:
            summary["ext_unknown"] += 1

    # 5. 写入 run + items
    if args.dry_run:
        print(f"   [dry-run] 统计: 总 {summary['total']} | 站内未覆盖 "
              f"{summary['int_missing']} | 外链死链 {summary['ext_dead']} | "
              f"外链未知 {summary['ext_unknown']}", flush=True)
        db.close()
        return

    run_id = db.start_run("linkcheck")
    t0 = time.time()
    for r in problems:
        if not (r["int_missing_count"] or r["ext_dead_count"]):
            continue
        doc_key = r["doc_key"]
        doc_url = ""
        d = db.get_doc(doc_key)
        if d:
            doc_url = d.get("url", "")
        r["url"] = doc_url
        db.add_item(run_id, doc_key, "problem", r)
    db.finish_run(run_id, {**summary, "elapsed_sec": round(time.time() - t0)})
    print(f"✅ 链接检查完成: 总 {summary['total']} 篇问题 | 站内未覆盖 "
          f"{summary['int_missing']} | 外链死链 {summary['ext_dead']} | "
          f"外链未知 {summary['ext_unknown']}", flush=True)
    db.close()


if __name__ == "__main__":
    main()