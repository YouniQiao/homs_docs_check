"""链接健康检查（真死链 / 被拒 / 服务端异常 / 不可达 / 锚点失效）。

相对旧版的修正：
  A. HTTP 请求**保留查询参数**（旧版 _normalize 会剥掉 ?query，导致带参链接被误判 400/404）
  B. **状态分桶**：dead(404/410) / blocked(被拒 400/401/403/405/406/418/422/429/451) /
     server(5xx，含 521/567) / unreachable(超时、解析失败)
  C. **直接 GET**（华为系与 CDN 普遍拒绝 HEAD），带完整浏览器头；403/405 再带 Referer 复测一次
  D. **url_cache 带 TTL**：ok/dead 缓存 7 天，其余（被拒/服务端异常/不可达）缓存 1 天，
     过期自动重查，避免瞬时错误被永久固化
  E. **锚点归一化对齐华为**（删除所有非字母数字字符后再比较 + 数字后缀回退），
     并豁免自动 id（section\d+ / li\d+ / p\d+ / h\d+ / 纯数字 等）

每篇有问题链接的文档一个 item（正常文档不记录）：
  detail: doc_key/lang/catalog/url、
          dead_links[]/blocked_links[]/server_links[]/unreachable_links[]
            {text,url,status,kind}  kind: int(华为系) / ext(外部)
          dead_count/blocked_count/server_count/unreachable_count、
          vintage_links[]/vintage_count、anchor_miss_links[]/anchor_miss_count

用法：
  python3 link_check.py            # 增量（读 sync 最新 run 的变更文档）
  python3 link_check.py --full     # 全量扫描
  python3 link_check.py --refresh  # 忽略缓存，强制重查
  python3 link_check.py --dry-run  # 只统计不写库
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Connection": "close",
}

# 状态分桶
DEAD_CODES = {"404", "410"}
BLOCKED_CODES = {"400", "401", "402", "403", "405", "406", "418", "422", "429", "451"}
RETRY_WITH_REFERER = {403, 405, 406, 429}   # 疑似反爬，换 Referer 再试一次

TTL_OK_DEAD = 7 * 86400      # 确定结果（可访问 / 真死）缓存 7 天
TTL_TRANSIENT = 1 * 86400    # 被拒 / 服务端异常 / 不可达 缓存 1 天


def _cache_key(url: str) -> str:
    """缓存键 / 去重键：仅去 fragment，保留查询参数（参数常常是访问必需）。"""
    return url.split("#")[0].rstrip()


def _is_internal(url: str) -> bool:
    return any(h in url for h in HUAWEI_HOSTS)


def _is_vintage(url: str) -> bool:
    """站内链接是否指向历史版本文档（如 harmonyos-guides-V5）。"""
    m = re.search(r"/doc/([^/]+)/", url)
    return bool(m and re.search(r"-V\d+$", m.group(1)))


def _bucket(status: str) -> str:
    """状态分桶：ok / dead / blocked / server / unreachable。"""
    s = str(status)
    if s.isdigit():
        c = int(s)
        if 200 <= c < 400:
            return "ok"
        if s in DEAD_CODES:
            return "dead"
        if s in BLOCKED_CODES:
            return "blocked"
        if 500 <= c < 600:
            return "server"
        return "blocked"      # 其他 4xx 统一归"被拒/需人工确认"
    return "unreachable"


def _ttl_for(status: str) -> int:
    return TTL_OK_DEAD if _bucket(status) in ("ok", "dead") else TTL_TRANSIENT


def _cache_fresh(entry: tuple[str, str] | None) -> bool:
    if not entry:
        return False
    status, updated_at = entry
    if not updated_at:
        return False
    try:
        ts = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return False
    return (time.time() - ts) < _ttl_for(status)


# ── 锚点处理（对齐华为的锚点规则：去标点、可能带序号）─────────────────────
def _anchor_slug(s: str) -> str:
    """删除所有非字母数字/中文字符（点、括号、连字符、下划线等一并删除）。
    如 'on(\\'gesturesShare\\')' -> 'ongesturesshare'，'image.createImageSource9+' ->
    'imagecreateimagesource9'，与华为站点的锚点 id 规则一致。"""
    s = s.replace("\\", "")
    s = re.sub(r"^\[h\d+\]", "", s)
    s = s.lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


def _strip_trailing_digits(s: str) -> str:
    return re.sub(r"\d+$", "", s)


def _doc_anchors(content: str) -> set[str]:
    heads = re.findall(r"^(?:#{1,6}|\[h\d+\])[ \t]*(.+)$", content, re.M)
    return {_anchor_slug(h) for h in heads if _anchor_slug(h)}


def _link_anchor(url: str) -> str | None:
    if "#" not in url:
        return None
    a = url.split("#", 1)[1].split("?", 1)[0].strip()
    a = re.split(r":~:", a)[0].strip()      # 去掉 Chrome 文本片段指令 :~:text=...
    if not a:
        return None
    a = urllib.parse.unquote(urllib.parse.unquote(a))   # 兼容双重编码
    return _anchor_slug(a)


AUTO_ID_RE = re.compile(
    r"^(?:section|table|li|p|h|tab|div|span|img|a|br|tr|td|figure|ch)\d+$")


def _is_auto_id(raw_anchor: str) -> bool:
    """无法在本地验证的锚点（站点自动生成 id / 乱码）。命中即"视为有效"。

    - 纯数字、section\\d+ / table\\d+ / li\\d+ / p\\d+ / ch\\d+ 等自动 id
    - 旧版主题 id：zh-cn_topic_* / en-us_topic_*
    - 旧版列表/段落自动 id：*_li<数字> / *_p<数字> / *_table<数字> …（如 en-us_topic_..._li1420045031813）
    - 含西里尔等非中英文字符（源文档 GBK→UTF-8 乱码，无法比对）
    """
    a = raw_anchor.strip()
    if re.fullmatch(r"\d+", a):
        return True
    if re.match(r"^(?:zh-cn|en-us)_topic_", a, re.I):   # 旧版主题 id（zh-cn_/en-us_，大小写不敏感）
        return True
    if re.search(r"_(?:li|p|table|ch|image|img|div|span|tr|td|figure|fig|h)\d{3,}$", a, re.I):
        return True                                # 旧版列表/段落自动 id
    if re.search(r"[\u0400-\u04ff]", a):           # 乱码锚点
        return True
    return bool(AUTO_ID_RE.fullmatch(a))


def _anchor_variants(s: str) -> set[str]:
    """一个 slug 的所有归一化变体：原形、去尾数字、去前导 section（华为锚点规则
    `section-<标题slug>`）；每种再去尾数字。"""
    out: set[str] = set()
    for base in (s, re.sub(r"^section-?", "", s)):
        if base:
            out.add(base)
            out.add(_strip_trailing_digits(base))
    return {x for x in out if x}


def _anchor_valid(slug: str, anchors: set[str]) -> bool:
    if not slug:
        return True
    sv = _anchor_variants(slug)
    for a in anchors:
        if sv & _anchor_variants(a):
            return True
    return False


# ── HTTP ────────────────────────────────────────────────────────────────
def _fetch_status(url: str, timeout: int = 12) -> str:
    """GET（带完整浏览器头）；403/405/406/429 疑似反爬时带 Referer 再试一次。"""
    last = "ERR:unknown"
    for extra in ({}, {"Referer": "https://developer.huawei.com/"}):
        try:
            req = urllib.request.Request(url, headers={**BROWSER_HEADERS, **extra})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return str(r.status)
        except urllib.error.HTTPError as e:
            last = str(e.code)
            if e.code not in RETRY_WITH_REFERER:
                return last
        except Exception as e:
            last = f"ERR:{type(e).__name__}"
    return str(last)


def _http_links(content: str) -> list[tuple[str, str, bool]]:
    """提取所有 http(s) 链接 [(链接文字, raw_url, 是否华为系)]。"""
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
    ap = argparse.ArgumentParser(description="链接健康检查（真死链/被拒/异常/锚点）")
    ap.add_argument("--full", action="store_true", help="全量扫描")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，强制重查所有 URL")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--workers", type=int, default=8, help="外部链接 HTTP 并发")
    ap.add_argument("--int-workers", type=int, default=1, help="站内链接并发（1=串行）")
    ap.add_argument("--int-delay", type=float, default=0.5, help="站内链接请求间隔(秒)")
    ap.add_argument("--timeout", type=int, default=12, help="HTTP 超时(秒)")
    ap.add_argument("--limit", type=int, default=0, help="最多文档数(调试)")
    args = ap.parse_args()

    db = IndexDB(BASE_DIR / "index.db")

    # 1. 收集文档
    if args.full:
        docs = collect_docs()
        print("📦 全量：扫描全部文档", flush=True)
    else:
        runs = db.list_runs("sync", limit=5)
        run = next((r for r in runs if r["status"] == "success"), None)
        keys = set()
        if run:
            for it in db.get_items(run["id"]):
                if it["item_type"] in ("added", "modified"):
                    keys.add(it["item_key"])
        docs = collect_docs(keys)
        print(f"📦 增量：sync run#{run['id'] if run else None} 变更 {len(keys)} 篇", flush=True)
    if args.limit:
        docs = docs[:args.limit]
    print(f"   待检查 {len(docs)} 篇", flush=True)

    # 预载 url->doc_key 映射（跨文档锚点检查用）+ 本次范围内文档的锚点
    doc_url_key: dict[str, str] = {
        (u or "").rstrip("/"): k
        for (k, u) in db._conn.execute("SELECT doc_key, url FROM docs") if u
    }
    # 锚点集：优先用同步时存的「源 HTML id」（= 站点真实锚点，权威依据）；
    # 没有（老数据/未采到）则退回本地 md 标题反推。按目标文档惰性查询 + 缓存。
    _local_paths = dict(db._conn.execute(
        "SELECT doc_key, local_path FROM docs WHERE local_path IS NOT NULL"))
    _acache: dict[str, set[str]] = {}
    _n_ids = db._conn.execute("SELECT COUNT(DISTINCT doc_key) FROM doc_anchors").fetchone()[0]
    print(f"   锚点源: {_n_ids} 篇用源 HTML id，其余退回 md 标题", flush=True)

    def anchors_for(key: str | None) -> set[str] | None:
        if not key:
            return None
        if key in _acache:
            return _acache[key]
        ids = db.get_doc_anchors(key)
        if ids:
            s = set(ids) | {_anchor_slug(a) for a in ids}
        else:
            p = _local_paths.get(key)
            if not p:
                return None          # 既无采集 id 又无本地文件 → 不校验（避免误报）
            try:
                s = _doc_anchors((BASE_DIR / p).read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                return None
        _acache[key] = s
        return s

    # 2. 收集唯一 URL（键=去 fragment、保留 query），按缓存 TTL 决定是否需重查
    t0 = time.time()
    need_int: set[str] = set()
    need_ext: set[str] = set()
    all_int: set[str] = set()
    all_ext: set[str] = set()
    vintage_urls: set[str] = set()
    for _dk, _lg, _ct, content in docs:
        for _text, raw, is_int in _http_links(content):
            u = _cache_key(raw)
            if not u:
                continue
            if is_int and _is_vintage(u):
                vintage_urls.add(u)
                continue
            (all_int if is_int else all_ext).add(u)
            entry = None if args.refresh else db.get_url_cache_entry(u)
            if not _cache_fresh(entry):
                (need_int if is_int else need_ext).add(u)
    print(f"   站内 URL {len(all_int)} 个（需查 {len(need_int)}） | "
          f"外部 URL {len(all_ext)} 个（需查 {len(need_ext)}） | "
          f"历史版本 URL {len(vintage_urls)} 个", flush=True)

    # 3. HTTP 检查（外部并发 / 站内低速串行），写缓存
    status: dict[str, str] = {}

    if need_ext:
        todo = sorted(need_ext)
        print(f"   [外部] HTTP 检查 {len(todo)} 个（并发 {args.workers}）", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_fetch_status, u, args.timeout): u for u in todo}
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
        done = 0
        for u in todo:
            try:
                status[u] = _fetch_status(u, args.timeout)
            except Exception:
                status[u] = "ERR:exec"
            db.set_url_cache(u, status[u])
            done += 1
            if done % 25 == 0:
                db.commit()          # 周期性落盘：中途中断也不丢已查结果（间隔小以缩短持锁时间）
            if done % 20 == 0 or done == len(todo):
                print(f"      站内进度: {done}/{len(todo)}", flush=True)
            if args.int_delay > 0:
                time.sleep(args.int_delay)

    for u, s in status.items():
        db.set_url_cache(u, s)
    db.commit()

    def get_status(u: str) -> str:
        if u in status:
            return status[u]
        return db.get_url_cache(u) or "ERR:nocache"

    # 4. 组装每文档问题（同文档内同一 URL 去重）
    problems: list[dict] = []
    for doc_key, lang, catalog, content in docs:
        dead: list[dict] = []
        blocked: list[dict] = []
        server: list[dict] = []
        unreach: list[dict] = []
        vintage: list[dict] = []
        anchor_miss: list[dict] = []
        seen: set[str] = set()
        for text, raw, is_int in _http_links(content):
            u = _cache_key(raw)
            if not u or u in seen:
                continue
            seen.add(u)
            if is_int and u in vintage_urls:
                vintage.append({"text": text, "url": raw, "kind": "int"})
                continue
            # 锚点失效（自锚点 + 华为系跨文档锚点）
            a_slug = _link_anchor(raw)
            if a_slug and (raw.startswith("#") or is_int):
                orig_a = raw.split("#", 1)[1].split("?", 1)[0].strip() if "#" in raw else ""
                if _is_auto_id(orig_a):
                    pass  # 自动 id，本地无法验证，视为有效
                else:
                    if raw.startswith("#"):
                        target_anchors = anchors_for(doc_key)
                    else:
                        target_key = doc_url_key.get(u)
                        target_anchors = anchors_for(target_key)
                    if target_anchors is not None and not _anchor_valid(a_slug, target_anchors):
                        anchor_miss.append({"text": text, "url": raw})
                        continue
            s = get_status(u)
            b = _bucket(s)
            rec = {"text": text, "url": raw, "status": s,
                   "kind": "int" if is_int else "ext"}
            if b == "dead":
                dead.append(rec)
            elif b == "blocked":
                blocked.append(rec)
            elif b == "server":
                server.append(rec)
            elif b == "unreachable":
                unreach.append(rec)
        if not (dead or blocked or server or unreach or vintage or anchor_miss):
            continue
        d = db.get_doc(doc_key)
        problems.append({
            "doc_key": doc_key, "lang": lang, "catalog": catalog,
            "url": d.get("url", "") if d else "",
            "dead_links": dead, "dead_count": len(dead),
            "blocked_links": blocked, "blocked_count": len(blocked),
            "server_links": server, "server_count": len(server),
            "unreachable_links": unreach, "unreachable_count": len(unreach),
            "vintage_links": vintage, "vintage_count": len(vintage),
            "anchor_miss_links": anchor_miss, "anchor_miss_count": len(anchor_miss),
        })

    # 5. 汇总 + 写库
    summary = {
        "checked": len(docs),
        "total": len(problems),
        "dead": sum(p["dead_count"] for p in problems),
        "blocked": sum(p["blocked_count"] for p in problems),
        "server": sum(p["server_count"] for p in problems),
        "unreachable": sum(p["unreachable_count"] for p in problems),
        "vintage": sum(p["vintage_count"] for p in problems),
        "anchor_miss": sum(p["anchor_miss_count"] for p in problems),
        "urls_checked": len(status),
    }
    print(f"   统计: {summary['total']} 篇问题 | 真死链 {summary['dead']} | "
          f"被拒 {summary['blocked']} | 服务端异常 {summary['server']} | "
          f"不可达 {summary['unreachable']} | 历史版本 {summary['vintage']} | "
          f"锚点失效 {summary['anchor_miss']}", flush=True)

    if args.dry_run:
        db.close()
        print("   [dry-run] 结束（未写库）", flush=True)
        return

    run_id = db.start_run("linkcheck")
    for p in problems:
        db.add_item(run_id, p["doc_key"], "dead", p)
    db.finish_run(run_id, {**summary, "elapsed_sec": round(time.time() - t0)})
    print(f"✅ 链接检查完成: run#{run_id}", flush=True)
    db.close()


if __name__ == "__main__":
    main()
