"""链接健康检查（真死链检查：外部 + 华为站内真实 HTTP）。

逐条链接真实 HTTP 访问，检测无法打开的页面：
  - dead（死链）：HTTP 状态码 >= 400（404/403/410/5xx 等）
  - unreachable（不可达）：连接超时 / 解析失败 / ERR

外部链接与华为站内链接都做真实 HTTP：
  - 华为站内（developer.huawei.com）对批量请求有反爬（并发>=16 连续大量会被 403），
    因此站内链接用**低速串行**检查（并发 --int-workers，请求间 --int-delay），避免被封。
  - 外部链接用常规并发（--workers）。
URL 结果写入 url_cache 表，多次运行（含每日增量）复用缓存，只对新增/未缓存 URL 做 HTTP，
支持断点续跑。

每篇有问题链接的文档一个 item（正常文档不记录，同 encheck）：
  detail: doc_key/lang/catalog/url、dead_links[]{text,url,status,kind}、
          dead_count、dead_int_count、dead_ext_count、unreachable_count。
  kind: int（华为站内）/ ext（外部）。

用法：
  python3 link_check.py            # 增量（读 sync 最新 run 的变更文档）
  python3 link_check.py --full     # 全量扫描所有文档
  python3 link_check.py --refresh  # 强制重查所有 URL
  python3 link_check.py --dry-run  # 只统计不写库
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
HUAWEI_HOSTS = ("developer.huawei.com", "contentcenter-videovali-drcn.dbankcdn.cn",
                "contentcenter-vali-drcn.dbankcdn.cn", "ohpm.openharmony.cn",
                "petalpay-merchant.cloud.huawei.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _normalize(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/")


def _is_internal(url: str) -> bool:
    return any(h in url for h in HUAWEI_HOSTS)


def _is_vintage(url: str) -> bool:
    """站内链接是否指向历史版本文档（catalog 含 -V5 等后缀，如 harmonyos-guides-V5）。

    正常文档不应链接到历史版本，这类链接是质量问题（非死链，页面仍可访问）。
    """
    m = re.search(r"/doc/([^/]+)/", url)
    return bool(m and re.search(r"-V\d+$", m.group(1)))


def _anchor_slug(s: str) -> str:
    """把标题/锚点文本归一化为可比的 slug：小写、去转义、去 [hN]、分隔符统一为 '-'、
    去标点，保留中文字符。如 'ArkUI\\_ErrorCode' / 'arkui_errorcode' -> 'arkui-errorcode'。
    """
    s = s.replace("\\", "")
    s = re.sub(r"^\[h\d+\]", "", s)
    s = s.lower()
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", s)
    s = re.sub(r"[-_]+", "-", s)
    return s.strip("-")


def _doc_anchors(content: str) -> set[str]:
    """提取文档所有标题的 slug 锚点集合（支持标准 '# ' 和 '\[hN\]' 两类标题）。"""
    heads = re.findall(r"^(?:#{1,6}|\[h\d+\])[ \t]*(.+)$", content, re.M)
    return {_anchor_slug(h) for h in heads if _anchor_slug(h)}


def _link_anchor(url: str) -> str | None:
    """提取链接的锚点 slug；无锚点返回 None。含 URL 解码（中文锚点可能是 URL 编码）。"""
    if "#" not in url:
        return None
    a = url.split("#", 1)[1]
    a = a.split("?", 1)[0].strip()
    if not a:
        return None
    # 中文锚点可能是 URL 编码（如 %E5%BC%80 或双重编码 %25E5），解码后再 slug
    a = urllib.parse.unquote(a)
    a = urllib.parse.unquote(a)
    return _anchor_slug(a)


def _anchor_valid(slug: str, anchors: set[str]) -> bool:
    if slug in anchors:
        return True
    # 前缀匹配：标题锚点可能带序号/后缀（如 xxx-1）
    if any(a.startswith(slug) for a in anchors):
        return True
    # API 重载/版本后缀回退：如 framenode-1(重载)、getSync12(API版本) 的基础是 framenode/getSync
    base = re.sub(r"-\d+$", "", slug)
    base = re.sub(r"\d+$", "", base)
    if base and (base in anchors or any(a.startswith(base) for a in anchors)):
        return True
    return False


def _http_status(url: str, timeout: int = 12) -> str:
    """HEAD 优先；HEAD 失败(403等)退 GET（华为拒绝 HEAD，返回 403，必须退回 GET）。"""
    last: str | int | None = None
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": UA,
                                                  "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return str(r.status)
        except urllib.error.HTTPError as e:
            last = e.code        # 400+，继续退 GET
        except Exception as e:
            last = f"ERR:{type(e).__name__}"
    return str(last)


def _http_links(content: str) -> list[tuple[str, str, bool]]:
    """提取所有 http 链接 [(链接文字, raw_url, 是否华为站内)]。"""
    out = []
    for text, url in LINK_RE.findall(content):
        if not url.startswith(("http://", "https://")):
            continue
        out.append((text, url, _is_internal(url)))
    return out


def collect_docs(doc_keys: set[str] | None = None) -> list[tuple]:
    """[(doc_key, lang, catalog, content), ...]"""
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
                docs.append((doc_key, lang, catalog, content))
    return docs


def main() -> None:
    ap = argparse.ArgumentParser(description="链接真死链检查（外部+站内）")
    ap.add_argument("--full", action="store_true", help="全量扫描")
    ap.add_argument("--refresh", action="store_true", help="强制重查所有 URL")
    ap.add_argument("--dry-run", action="store_true", help="只统计")
    ap.add_argument("--workers", type=int, default=8, help="外部链接 HTTP 并发")
    ap.add_argument("--int-workers", type=int, default=1,
                    help="站内链接并发（1=串行；华为反爬需低速率，>=2 高频会 403）")
    ap.add_argument("--int-delay", type=float, default=0.5,
                    help="站内链接请求间隔(秒)；串行+0.5~1s 已验证可绕过反爬")
    ap.add_argument("--timeout", type=int, default=12, help="HTTP 超时(秒)")
    ap.add_argument("--limit", type=int, default=0, help="最多文档数(调试)")
    args = ap.parse_args()

    db = IndexDB(BASE_DIR / "index.db")

    # 1. 收集文档
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

    # 预载全量文档标题锚点 + url->doc_key 映射（锚点失效检查用）
    doc_url_key: dict[str, str] = {
        (u or "").rstrip("/"): k
        for (k, u) in db._conn.execute("SELECT doc_key, url FROM docs") if u
    }
    doc_anchors: dict[str, set[str]] = {
        key: _doc_anchors(content) for key, _lg, _ct, content in docs
    }

    # 2. 收集唯一 URL，分类外部/站内，确定需 HTTP 的
    t0 = time.time()
    need_int: set[str] = set()
    need_ext: set[str] = set()
    all_int: set[str] = set()
    all_ext: set[str] = set()
    vintage_urls: set[str] = set()
    for _dk, _lg, _ct, content in docs:
        for _text, raw, is_int in _http_links(content):
            u = _normalize(raw)
            if is_int and _is_vintage(u):
                vintage_urls.add(u)   # 历史版本链接：本地判定，不 HTTP
                continue
            (all_int if is_int else all_ext).add(u)
            if args.refresh or db.get_url_cache(u) is None:
                (need_int if is_int else need_ext).add(u)
    print(f"   站内链接 URL {len(all_int)} 个（需查 {len(need_int)}） | "
          f"外部 URL {len(all_ext)} 个（需查 {len(need_ext)}） | "
          f"历史版本链接 URL {len(vintage_urls)} 个", flush=True)

    # 3. HTTP 检查（外部并发 / 站内低速串行），写缓存
    status: dict[str, str] = {}

    if need_ext:
        todo = sorted(need_ext)
        print(f"   [外部] HTTP 检查 {len(todo)} 个（并发 {args.workers}）",
              flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_http_status, u, args.timeout): u for u in todo}
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                try:
                    status[u] = fut.result()
                except Exception:
                    status[u] = "ERR:exec"

    if need_int:
        todo = sorted(need_int)
        print(f"   [站内] HTTP 检查 {len(todo)} 个（并发 {args.int_workers}，"
              f"间隔 {args.int_delay}s 防反爬）", flush=True)
        n = done = 0
        for u in todo:
            try:
                status[u] = _http_status(u, args.timeout)
            except Exception:
                status[u] = "ERR:exec"
            db.set_url_cache(u, status[u])
            n += 1
            done += 1
            if n % 20 == 0 or done == len(todo):
                print(f"      站内进度: {done}/{len(todo)}", flush=True)
            # 每请求后固定间隔（必须在串行下也生效，否则被华为 403）
            if args.int_delay > 0:
                time.sleep(args.int_delay)

    # 写缓存（站内已边查边写；外部批量补写）
    for u, s in status.items():
        db.set_url_cache(u, s)
    db.commit()

    def get_status(u: str) -> str:
        if u in status:
            return status[u]
        c = db.get_url_cache(u)
        return c or "ERR:nocache"

    # 4. 组装每文档死链
    summary = {"total": 0, "int_dead": 0, "ext_dead": 0, "unreachable": 0}
    problems: list[dict] = []
    for doc_key, lang, catalog, content in docs:
        dead, unk, vintage, anchor_miss = [], 0, [], []
        for text, raw, is_int in _http_links(content):
            u = _normalize(raw)
            if is_int and u in vintage_urls:
                vintage.append({"text": text, "url": raw})
                continue
            # 锚点失效检查（自锚点 + 站内跨文档锚点）
            a_slug = _link_anchor(raw)
            if a_slug and (raw.startswith("#") or is_int):
                orig_a = raw.split("#", 1)[1].split("?", 1)[0].strip()
                if re.fullmatch(r"section\d+", orig_a):
                    continue  # 华为自动内容块 id，本地转换丢失无法验证，视为有效
                if raw.startswith("#"):
                    target_anchors = doc_anchors.get(doc_key)
                else:
                    target_anchors = doc_anchors.get(doc_url_key.get(u))
                if target_anchors is not None and \
                        not _anchor_valid(a_slug, target_anchors):
                    anchor_miss.append({"text": text, "url": raw})
                    continue
            s = get_status(u)
            if s.isdigit() and int(s) >= 400:
                dead.append({"text": text, "url": raw, "status": s,
                             "kind": "int" if is_int else "ext"})
            elif s.startswith("ERR") or s == "ERR:nocache":
                unk += 1
        if not dead and not unk and not vintage and not anchor_miss:
            continue
        doc_url = ""
        d = db.get_doc(doc_key)
        if d:
            doc_url = d.get("url", "")
        dead_int = sum(1 for x in dead if x["kind"] == "int")
        dead_ext = sum(1 for x in dead if x["kind"] == "ext")
        problems.append({"doc_key": doc_key, "lang": lang, "catalog": catalog,
                         "url": doc_url, "dead_links": dead, "dead_count": len(dead),
                         "dead_int_count": dead_int, "dead_ext_count": dead_ext,
                         "unreachable_count": unk,
                         "vintage_links": vintage, "vintage_count": len(vintage),
                         "anchor_miss_links": anchor_miss,
                         "anchor_miss_count": len(anchor_miss)})

    # 5. 汇总 + 写库
    summary["total"] = len(problems)
    summary["int_dead"] = sum(p["dead_int_count"] for p in problems)
    summary["ext_dead"] = sum(p["dead_ext_count"] for p in problems)
    summary["unreachable"] = sum(p["unreachable_count"] for p in problems)
    summary["vintage"] = sum(p["vintage_count"] for p in problems)
    summary["anchor_miss"] = sum(p["anchor_miss_count"] for p in problems)
    print(f"   统计: {summary['total']} 篇问题 | 站内死链 "
          f"{summary['int_dead']} | 外部死链 {summary['ext_dead']} | "
          f"不可达 {summary['unreachable']} | 历史版本链接 "
          f"{summary['vintage']} | 锚点失效 {summary['anchor_miss']}", flush=True)

    if args.dry_run:
        db.close()
        print("   [dry-run] 结束（未写库）", flush=True)
        return

    run_id = db.start_run("linkcheck")
    for p in problems:
        db.add_item(run_id, p["doc_key"], "dead", p)
    db.finish_run(run_id, {**summary, "elapsed_sec": round(time.time() - t0),
                           "urls_checked": len(status)})
    print(f"✅ 链接检查完成: run#{run_id}", flush=True)
    db.close()


if __name__ == "__main__":
    main()