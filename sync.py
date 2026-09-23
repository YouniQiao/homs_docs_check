"""HarmonyOS 文档同步主脚本。

用法：
  python3 sync.py              # 增量同步
  python3 sync.py --full       # 首次全量（建立基线，不记录变更清单）
  python3 sync.py --catalog harmonyos-guides --lang en   # 限定范围
  python3 sync.py --dry-run    # 只对比不下载
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from hw_api import get_catalog_tree, collect_documents, get_document
from db import IndexDB
from converter import convert_and_localize
import requests

BASE_DIR = Path(__file__).resolve().parent

# thread-local session：每个 worker 线程复用连接池，避免并发下新建 TCP 连接
_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://developer.huawei.com/consumer/en/doc/",
        })
        _local.session = s
    return _local.session


# 图片 URL 的动态 query 参数（HW-CC-Date / HW-CC-Sign 签名有时效，随时间变化）
_QUERY_RE = re.compile(r'\?[^"\'>\s]+')

# 下载链接的时效签名（URL 路径里，形如 :20260819120129:2800:203C3027...）：
# 14 位时间戳 + 数字 + 40+ 位 hex 签名，随时间变化
_DL_TOKEN_RE = re.compile(r':\d{14}:\d+:[A-F0-9]{40,}')


def stable_hash(html: str) -> str:
    """计算 HTML 内容的稳定 sha256（剔除动态部分）。

    华为文档 HTML 里有两类动态字段，直接 hash 会导致增量误判：
    1. 图片 URL 的时效签名 query（HW-CC-Date/Sign）
    2. 下载链接的时效签名 token（URL 路径里的 :时间戳:数字:hex签名）
    剔除后只保留稳定内容（正文、表格、链接、图片 base URL）。
    """
    html = _QUERY_RE.sub("", html)
    html = _DL_TOKEN_RE.sub("", html)
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Kit / IDE 分组映射（由 sysmerge/kit_map.py 从目录树推导，只读）────────────
# 惰性加载一次，供 store_document 写入新文档时带上 kit/ide；映射文件缺失或损坏
# 时退化为空表（新文档 kit/ide 写 NULL），绝不影响同步主流程。
_KIT_IDE_LOCK = threading.Lock()
_KIT_IDE_MAPS: tuple[dict, dict] | None = None

KIT_MAP_PATH = BASE_DIR / "sysmerge" / "kit_map.v2.json"
IDE_MAP_PATH = BASE_DIR / "sysmerge" / "ide_map.json"


def _load_kit_ide_maps() -> tuple[dict, dict]:
    """返回 ({doc_key: kit}, {doc_key: ide})；只加载一次（线程安全）。"""
    global _KIT_IDE_MAPS
    if _KIT_IDE_MAPS is None:
        with _KIT_IDE_LOCK:
            if _KIT_IDE_MAPS is None:
                maps = []
                for path in (KIT_MAP_PATH, IDE_MAP_PATH):
                    try:
                        with open(path, encoding="utf-8") as f:
                            maps.append(json.load(f))
                    except (OSError, ValueError) as e:
                        print(f"   ⚠️  Kit/IDE 映射读取失败 {path.name}: {e}", flush=True)
                        maps.append({})
                _KIT_IDE_MAPS = (maps[0], maps[1])
    return _KIT_IDE_MAPS


def lookup_kit_ide(doc_key: str) -> tuple[str | None, str | None]:
    """按 doc_key 查 (kit, ide_group)；查不到为 None（写库即 NULL）。"""
    kit_map, ide_map = _load_kit_ide_maps()
    return kit_map.get(doc_key), ide_map.get(doc_key)


def doc_url(lang: str, catalog: str, file_name: str) -> str:
    return f"https://developer.huawei.com/consumer/{lang}/doc/{catalog}/{file_name}"


def build_catalog_docs(config: dict, catalogs: list[str], langs: list[str]) -> list[dict]:
    """拉取所有 catalog 的目录树，收集文档清单。

    返回 [{lang, catalog, relate_document, title}]。
    """
    docs = []
    for catalog in catalogs:
        for lang in langs:
            print(f"  📂 拉取目录树: {catalog} ({lang})", flush=True)
            tree = get_catalog_tree(catalog, lang)
            for d in collect_documents(tree):
                docs.append({
                    "lang": lang,
                    "catalog": catalog,
                    "relate_document": d["relate_document"],
                    "title": d["title"],
                })
    return docs


def store_document(doc: dict, config: dict, value: dict) -> dict | None:
    """用已拿到的 value 转换并存盘（文件）。不写 SQLite，供并发 worker 调用。

    返回索引条目 dict（含 title/url/img_count/content_hash），失败返回 None。
    """
    lang = doc["lang"]
    catalog = doc["catalog"]
    rel = doc["relate_document"]
    doc_key = f"{lang}|{catalog}|{rel}"
    kit, ide = lookup_kit_ide(doc_key)

    doc_dir = BASE_DIR / config["data_dir"] / lang / catalog
    doc_dir.mkdir(parents=True, exist_ok=True)
    md_path = doc_dir / f"{rel}.md"
    images_dir = doc_dir / "images"

    title = value.get("title", doc.get("title", ""))
    file_name = value.get("file_name", rel)
    updated = value.get("updatedDate", "")
    display_time = value.get("displayUpdateTime", "")
    html = value["content"].get("content", "")
    content_hash = stable_hash(html)

    # 站点锚点 = 源 HTML 里的 id=（新版）+ <a name=（旧版页面）——链接锚点校验的权威依据
    anchor_ids = sorted(set(re.findall(r'id="([^"]+)"', html))
                        | set(re.findall(r'<a[^>]+name="([^"]+)"', html)))

    # 转换 + 图片本地化（用 thread-local session，连接复用）
    markdown, img_count = convert_and_localize(html, images_dir, get_session())

    # 存盘
    md_path.write_text(markdown, encoding="utf-8")

    return {
        "doc_key": doc_key,
        "lang": lang,
        "catalog": catalog,
        "relate_document": rel,
        "title": title,
        "file_name": file_name,
        "updated_date": updated,
        "display_update_time": display_time,
        "content_hash": content_hash,
        "local_path": str(md_path.relative_to(BASE_DIR)),
        "url": doc_url(lang, catalog, file_name),
        "last_synced": datetime.now().isoformat(timespec="seconds"),
        "kit": kit,
        "ide": ide,
        "img_count": img_count,
        "anchor_ids": anchor_ids,
    }


def fetch_document(doc: dict, config: dict) -> dict | None:
    """拉取单篇文档（调 API），转换并存盘。供新增文档的并发 worker 调用。"""
    value = get_document(doc["relate_document"], doc["catalog"], doc["lang"])
    if not value or not value.get("content"):
        print(f"    ⚠️  无内容: {doc['relate_document']}", flush=True)
        return None
    return store_document(doc, config, value)


def _check_updated(d: dict, old_display_time: str) -> tuple[dict, dict] | None:
    """并发检查单篇文档是否更新（displayUpdateTime 对比）。

    displayUpdateTime 是页面展示的"更新时间"，反映真实内容更新时间：
    - 不受 updatedDate 批量 touch 影响（touch 只改后端字段）
    - 不受 HTML 里图片/下载链接签名 token 影响（token 刷新不改页面时间）
    返回 (doc, value) 若内容变化（value 复用给下载阶段，省二次 API 调用）。
    """
    try:
        value = get_document(d["relate_document"], d["catalog"], d["lang"])
        new_display_time = value.get("displayUpdateTime", "")
        if new_display_time and new_display_time != old_display_time:
            return (d, value)
    except Exception as e:
        print(f"    ⚠️  检查 {d['relate_document']} 失败: {e}", flush=True)
    return None


def sync(config: dict, db: IndexDB, catalogs: list[str], langs: list[str],
         is_full: bool = False, dry_run: bool = False, delay: float = 0.1,
         workers: int = 8):
    """执行同步。返回 (added, modified, deleted)。"""
    print(f"🌐 开始{'全量' if is_full else '增量'}同步", flush=True)
    print(f"   catalogs: {catalogs}", flush=True)
    print(f"   langs: {langs}", flush=True)
    print(f"   并发: {workers} 线程", flush=True)

    # 1. 拉目录树
    current_docs = build_catalog_docs(config, catalogs, langs)
    current_keys = {f"{d['lang']}|{d['catalog']}|{d['relate_document']}" for d in current_docs}
    print(f"   当前官网文档总数: {len(current_docs)}", flush=True)

    # 2. 对比本地索引（仅当前同步范围内的本地文档参与对比，
    #    避免 --catalog/--lang 限定范围时把范围外文档误判为删除）
    local_keys = db.get_all_keys()
    local_keys_in_scope = {
        k for k in local_keys
        if k.split("|", 2)[0] in langs and k.split("|", 2)[1] in catalogs
    }
    added_keys = current_keys - local_keys_in_scope
    deleted_keys = local_keys_in_scope - current_keys
    common_keys = current_keys & local_keys_in_scope

    print(f"   本地已有: {len(local_keys)} (范围内 {len(local_keys_in_scope)}) | "
          f"新增: {len(added_keys)} | 删除: {len(deleted_keys)} | "
          f"需检查更新: {len(common_keys)}", flush=True)

    if dry_run:
        print("   [dry-run] 跳过实际下载", flush=True)
        return len(added_keys), 0, len(deleted_keys)

    run_id = db.start_run("sync")
    added = modified = deleted = 0

    # 3. 处理删除
    for key in sorted(deleted_keys):
        lang, catalog, rel = key.split("|")
        old = db.get_doc(key)
        db.mark_deleted(key)
        if not is_full:
            db.add_item(run_id, key, "deleted", {
                "lang": lang, "catalog": catalog,
                "title": old.get("title", "") if old else rel,
                "url": old.get("url", "") if old else doc_url(lang, catalog, rel),
            })
        deleted += 1

    # 4. 处理新增
    to_fetch = []  # (doc_dict, change_type, value_or_None)
    for d in current_docs:
        key = f"{d['lang']}|{d['catalog']}|{d['relate_document']}"
        if key in added_keys:
            to_fetch.append((d, "added", None))

    # 5. 并发检查 common 的更新（displayUpdateTime 对比，反映真实内容更新）
    if not is_full and common_keys:
        print(f"   🔍 并发检查 {len(common_keys)} 篇已有文档的更新...", flush=True)
        common_docs = []
        for d in current_docs:
            key = f"{d['lang']}|{d['catalog']}|{d['relate_document']}"
            if key in common_keys:
                old = db.get_doc(key)
                common_docs.append(
                    (d, old.get("display_update_time", "") if old else ""))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_check_updated, d, old_display_time): d
                for d, old_display_time in common_docs
            }
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                done += 1
                result = fut.result()
                if result:
                    d2, value = result
                    to_fetch.append((d2, "modified", value))
                if done % 500 == 0 or done == len(common_docs):
                    print(f"   检查进度: {done}/{len(common_docs)}", flush=True)

    # 6. 并发下载新增 + 变更（modified 复用检查阶段已拿到的 value，省二次 API）
    print(f"   ⬇️  并发下载 {len(to_fetch)} 篇（新增+变更）...", flush=True)
    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for d, change_type, value in to_fetch:
                if value is not None:
                    fut = pool.submit(store_document, d, config, value)
                else:
                    fut = pool.submit(fetch_document, d, config)
                futures[fut] = (d, change_type)
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                d, change_type = futures[fut]
                done += 1
                try:
                    entry = fut.result()
                    if entry:
                        entry.pop("img_count", None)
                        ids = entry.pop("anchor_ids", None)
                        if ids is not None:
                            db.set_doc_anchors(entry["doc_key"], ids)
                        db.upsert_doc(entry)
                        if change_type == "added":
                            added += 1
                        else:
                            modified += 1
                        if not is_full:
                            db.add_item(
                                run_id,
                                f"{d['lang']}|{d['catalog']}|{d['relate_document']}",
                                change_type, {
                                    "lang": d["lang"], "catalog": d["catalog"],
                                    "title": entry["title"], "url": entry["url"],
                                })
                except Exception as e:
                    print(f"    ❌ 下载失败 {d['relate_document']}: {e}", flush=True)
                if done % 50 == 0 or done == len(to_fetch):
                    print(f"   进度: {done}/{len(to_fetch)} "
                          f"(added={added} modified={modified})", flush=True)

    db.commit()
    db.finish_run(run_id, {"added": added, "modified": modified, "deleted": deleted})
    print(f"✅ 同步完成: 新增 {added} | 修改 {modified} | 删除 {deleted}", flush=True)
    return added, modified, deleted


def main():
    parser = argparse.ArgumentParser(description="HarmonyOS 文档同步")
    parser.add_argument("--full", action="store_true", help="首次全量（建立基线）")
    parser.add_argument("--dry-run", action="store_true", help="只对比不下载")
    parser.add_argument("--catalog", action="append", help="限定 catalog（可多次）")
    parser.add_argument("--lang", action="append", help="限定语言（可多次）")
    parser.add_argument("--workers", type=int, default=None,
                        help="并发线程数（默认取 config.yaml 的 workers）")
    args = parser.parse_args()

    config = load_config()
    catalogs = args.catalog or config["catalogs"]
    langs = args.lang or config["languages"]
    delay = config.get("request_delay", 0.1)
    workers = args.workers or config.get("workers", 8)

    db = IndexDB(str(BASE_DIR / config["db_path"]))
    try:
        sync(config, db, catalogs, langs, is_full=args.full,
             dry_run=args.dry_run, delay=delay, workers=workers)
    finally:
        db.close()


if __name__ == "__main__":
    main()
