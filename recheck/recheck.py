#!/usr/bin/env python3
"""当前全量问题（module_key='recheck'）：把**全部历史发现的问题**重新核一遍，看是否已被修复。

页面形态（用户口径）：
  · 按模块分开，用**各模块原来的列/样式**呈现（页签切换）；
  · 明细**只保留"仍有问题"的项**；已解决 / 已失效只在顶部数字与解决率里体现。
汇总口径（用户口径）：解决率 = 已解决 / (已解决 + 仍存在 + 已失效)，失效也计入分母。

三态判定：
  resolved 已解决 —— 问题消失，且复核对象仍存在
  still    仍存在 —— 问题依旧
  gone     已失效 —— 复核对象已不存在（文档/图片被删、图片/链接不再被任何文档引用）

用法:
  python3 recheck/recheck.py                       # 全部（linkcheck+encheck+ocr）
  python3 recheck/recheck.py --module linkcheck
  python3 recheck/recheck.py --dry-run --limit 20
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402
from linkcheck.link_check import (  # noqa: E402
    _anchor_slug, _anchor_valid, _bucket, _cache_key, _doc_anchors, _is_auto_id,
    _is_vintage, _link_anchor, _fetch_status,
)
from encheck.en_check import check_md  # noqa: E402
from imgnorm.rules import FIELD, RULES, analyze  # noqa: E402

import ignores  # noqa: E402

# verdict 的 ptype / 计数字段 → 忽略 kind
LINK_KIND = {"断链": "dead", "误链历史版本": "vintage", "锚点失效": "anchor_miss"}
EN_KIND = {"hanzi_count": "hanzi", "punct_count": "punct",
           "url_cn_char_count": "url_cn", "cn_link_count": "cn_link"}
DATA_DIR = BASE_DIR / "data"

# 复核范围：imgnorm（图片内容规范）暂不参与（用户要求），代码保留在 ALL_MODULES 里备启用
ALL_MODULES = ("linkcheck", "encheck", "ocr", "imgnorm")
# 顺序与首页「每日增量内容检查」分组一致（图片 OCR → 英文文档 → 链接健康；imgnorm 未参与）
MODULES = ("ocr", "encheck", "linkcheck")
MODULE_LABEL = {"linkcheck": "链接健康检查", "encheck": "英文文档检查",
                "ocr": "图片 OCR 检查", "imgnorm": "图片内容规范检查"}
_BUCKET_LABEL = {"blocked": "被拒", "server": "服务端异常", "unreachable": "不可达"}

IMG_RE = re.compile(r"!\[[^\]]*\]\(images/([^)\s\"]+)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


# ───────────────────────────────────────────── 收集目标 + 引用集
def collect_targets(db) -> dict:
    """把历史（跨所有 run 去重）发现的"问题"收集成待复核目标。

    模块归属以 **run.module_key** 为准（不再靠字段猜，避免把 imgnorm 项错当 ocr 项）；
    OCR 只收「英文文档 + 含中文」（has_cn）的图——中文文档含中文是正常的。
    """
    t = {"linkcheck": {}, "encheck": set(), "ocr": set(), "imgnorm": set()}
    rows = db._conn.execute(
        "SELECT r.module_key, i.item_key, i.item_type, i.detail_json "
        "FROM items i JOIN runs r ON i.run_id=r.id "
        "WHERE r.module_key IN ('linkcheck','encheck','ocr','imgnorm') "
        "ORDER BY i.id").fetchall()
    for mk, item_key, item_type, dj in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        if mk == "linkcheck":
            dk = d.get("doc_key") or item_key
            for field, ptype in (("dead_links", "断链"), ("vintage_links", "误链历史版本"),
                                 ("anchor_miss_links", "锚点失效")):
                for l in (d.get(field) or []):
                    url = l.get("url") if isinstance(l, dict) else l
                    if not url:
                        continue
                    text = l.get("text", "") if isinstance(l, dict) else ""
                    key = (dk, url, ptype) if url.startswith("#") else (url, ptype)
                    t["linkcheck"][key] = (dk, url, ptype, text)
        elif mk == "encheck":
            if d.get("doc_key"):
                t["encheck"].add(d["doc_key"])
        elif mk == "ocr":
            # 只有「英文文档 + 含中文」才算历史问题（item_type=has_cn）
            if d.get("image") and d.get("lang") == "en" and \
                    (item_type == "has_cn" or d.get("has_cn") is True):
                t["ocr"].add(d["image"])
        elif mk == "imgnorm":
            if d.get("image"):
                t["imgnorm"].add(d["image"])
    return t


def scan_docs() -> tuple[set, set, dict]:
    """扫描全部文档，得到当前仍被引用的 URL / 图片集合，以及 图片->引用它的文档 doc_key。"""
    urls: set = set()
    imgs: set = set()
    img_doc: dict[str, str] = {}
    for cat_dir in DATA_DIR.rglob("*"):
        if not cat_dir.is_dir():
            continue
        for md in cat_dir.glob("*.md"):
            try:
                txt = md.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = md.relative_to(DATA_DIR).parts
            lang = rel[0]
            catalog = rel[1] if len(rel) > 1 else ""
            doc_key = f"{lang}|{catalog}|{md.stem}"
            for m in IMG_RE.finditer(txt):
                p = f"data/{lang}/{catalog}/images/{m.group(1)}"
                imgs.add(p)
                img_doc.setdefault(p, doc_key)
            for m in LINK_RE.finditer(txt):
                urls.add(_cache_key(m.group(2).strip()))
    return urls, imgs, img_doc


def _doc_meta(db) -> dict:
    """doc_key -> (doc_url, title, lang, catalog)。"""
    meta = {}
    try:
        for dk, url, title, lang, cat in db._conn.execute(
                "SELECT doc_key, url, title, lang, catalog FROM docs"):
            meta[dk] = (url or "", title or "", lang or "", cat or "")
    except Exception:
        pass
    return meta


def _doc_key_url(doc_key: str) -> tuple[str, str]:
    """从 doc_key 推 (base_url, catalog)。cn|cat|name -> https://developer.huawei.com/consumer/cn/doc/cat/name"""
    parts = (doc_key or "").split("|")
    if len(parts) != 3:
        return "", ""
    lang, catalog, name = parts
    return f"https://developer.huawei.com/consumer/{lang}/doc/{catalog}/{name}", catalog


# ───────────────────────────────────────────── 各模块复核（产出 verdict）
def _verdict(module, ptype, target, status, evidence, **kw):
    v = {"module": module, "ptype": ptype, "target": target,
         "status": status, "evidence": evidence}
    v.update(kw)
    return v


def verdicts_linkcheck(db, tg, urls_ref, args, meta, rules) -> list:
    """逐条复核历史链接问题：锚点/历史版本本地判、断链才发请求。已忽略的直接跳过。"""
    out, dead_todo = [], []
    doc_cache: dict[str, str | None] = {}
    url_key = {}   # cache_key -> doc_key（本地文档 URL 映射）
    try:
        for dk, url in db._conn.execute("SELECT doc_key, url FROM docs WHERE url IS NOT NULL"):
            if url:
                url_key[_cache_key(url.rstrip("/"))] = dk
    except Exception:
        pass

    def content_of(dk):
        if dk in doc_cache:
            return doc_cache[dk]
        p = meta.get(dk, ("", "", "", ""))[0]
        lp = None
        try:
            r = db._conn.execute("SELECT local_path FROM docs WHERE doc_key=?", (dk,)).fetchone()
            lp = r[0] if r else None
        except Exception:
            pass
        txt = None
        if lp and Path(lp).exists():
            try:
                txt = Path(lp).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                txt = None
        doc_cache[dk] = txt
        return txt

    anchor_cache: dict[str, set[str]] = {}

    def anchors_of(dk: str, txt: str) -> set[str]:
        """锚点集：优先用同步时存的源 HTML id（= 站点真实锚点）；缺则退回 md 标题。"""
        if dk in anchor_cache:
            return anchor_cache[dk]
        try:
            ids = db.get_doc_anchors(dk)
        except Exception:
            ids = set()
        s = (set(ids) | {_anchor_slug(a) for a in ids}) if ids else _doc_anchors(txt)
        anchor_cache[dk] = s
        return s

    for (dk, url, ptype, text) in tg["linkcheck"].values():
        rec = _verdict("linkcheck", ptype, url, None, "",
                       doc_key=dk, text=text)
        if ignores.is_ignored(rules, "linkcheck", url, LINK_KIND.get(ptype, ""), dk):
            # 已忽略：不参与复核、不计入解决率，也不再发请求
            rec.update(status="ignored", evidence="已忽略")
            out.append(rec)
            continue
        if _cache_key(url) not in urls_ref and not url.startswith("#"):
            rec.update(status="gone", evidence="链接已不再被任何文档引用")
            out.append(rec)
            continue
        if ptype == "锚点失效":
            raw = url
            if raw.startswith("#"):
                target_dk = dk
            else:
                target_dk = url_key.get(_cache_key(raw.split("#")[0].rstrip("/")))
            if not target_dk:
                rec.update(status="gone", evidence="目标文档已不存在")
            else:
                txt = content_of(target_dk)
                if txt is None:
                    rec.update(status="gone", evidence="目标文档本地文件缺失")
                else:
                    # 注意：_is_auto_id 要用「原始锚点串」判断（可识别 zh-cn_topic_*、
                    # 乱码、section\d+ 等无法本地验证的锚点）——传归一化后的 slug 会漏判
                    orig_a = raw.split("#", 1)[1].split("?", 1)[0].strip() if "#" in raw else ""
                    slug = _link_anchor(raw)
                    if not slug or _is_auto_id(orig_a):
                        rec.update(status="resolved",
                                   evidence="锚点为站点自动生成/无法本地验证，视为有效")
                    else:
                        ok = _anchor_valid(slug, anchors_of(target_dk, txt))
                        rec.update(status="resolved" if ok else "still",
                                   evidence="锚点已存在" if ok else "锚点仍缺失")
            out.append(rec)
        elif ptype == "误链历史版本":
            if _is_vintage(url):
                rec.update(status="still", evidence="仍指向历史版本文档")
            else:
                rec.update(status="resolved", evidence="已不再指向历史版本")
            out.append(rec)
        else:   # 断链
            dead_todo.append(rec)

    if dead_todo:
        print(f"   [linkcheck] 重新请求断链 {len(dead_todo)} 个（间隔 {args.int_delay}s）", flush=True)
        for i, rec in enumerate(dead_todo, 1):
            url = rec["target"]
            try:
                st = _fetch_status(url, args.timeout)
            except Exception:
                st = "ERR:exec"
            b = _bucket(st)
            if b == "ok":
                rec["status"] = "resolved"
                rec["evidence"] = f"HTTP {st}（已恢复）"
            elif b == "dead":
                rec["status"] = "still"
                rec["evidence"] = f"HTTP {st}（仍为断链）"
            else:
                # 被拒/服务端异常/不可达 = 已不是断链（反爬拦截或临时故障），不计入"断链仍存在"
                rec["status"] = "resolved"
                rec["evidence"] = f"HTTP {st}（非断链：{_BUCKET_LABEL.get(b, b)}）"
            out.append(rec)
            if args.int_delay:
                time.sleep(args.int_delay)
            if i % 20 == 0:
                print(f"      {i}/{len(dead_todo)}", flush=True)
    return out


def verdicts_encheck(db, tg, meta, args, rules) -> list:
    """逐篇复核英文文档：文档没了→gone；仍有中文→still，否则→resolved。已忽略的问题类型不计入。"""
    out = []
    fresh: dict[str, tuple] = {}
    for dk in sorted(tg["encheck"]):
        rec = _verdict("encheck", "文档中文", dk, None, "", doc_key=dk)
        lp = None
        try:
            r = db._conn.execute("SELECT local_path FROM docs WHERE doc_key=?", (dk,)).fetchone()
            lp = r[0] if r else None
        except Exception:
            pass
        p = Path(lp) if lp else None
        if not p or not p.exists():
            rec.update(status="gone", evidence="文档已不存在")
            out.append(rec)
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            rec.update(status="gone", evidence=f"读取失败: {e}")
            out.append(rec)
            continue
        res = check_md(content)
        counts = {"hanzi_count": res[1], "punct_count": res[3],
                  "url_cn_char_count": res[5], "cn_link_count": res[8]}
        fresh[dk] = res
        pairs = (("hanzi_count", "含汉字"), ("punct_count", "含标点"),
                 ("url_cn_char_count", "链接URL含中文"), ("cn_link_count", "含中文链接"))
        hit = [k for k, _l in pairs if counts[k] > 0]

        def _kind_ignored(k):
            """该类型是否已被忽略：中文链接逐条看（全部链接都被忽略才算），其余按文档。"""
            kind = EN_KIND[k]
            if k == "cn_link_count":
                urls = [l.get("url") for l in (res[7] or []) if isinstance(l, dict) and l.get("url")]
                return bool(urls) and all(
                    ignores.is_ignored(rules, "encheck", u, kind) for u in urls if u)
            return ignores.is_ignored(rules, "encheck", dk, kind)

        active = [k for k in hit if not _kind_ignored(k)]
        ign = [k for k in hit if _kind_ignored(k)]
        if active:
            labels = [lab for k, lab in pairs if k in active]
            rec.update(status="still", evidence="仍存在：" + "、".join(labels),
                       extra={"check": res})
        elif ign:
            rec.update(status="ignored",
                       evidence="已忽略：" + "、".join(lab for k, lab in pairs if k in ign))
        else:
            rec.update(status="resolved", evidence="文档已无中文问题")
        out.append(rec)
    return out


def _ocr_worker(batch):
    from ocr.ocr_engine import PaddleOcrEngine
    eng = PaddleOcrEngine()
    res = []
    for p in batch:
        try:
            r = eng.recognize(str(p))
            res.append((p, r))
        except Exception as e:
            res.append((p, ("", [], 0.0, str(e))))
    return res


def verdicts_ocr(db, tg, imgs_ref, img_doc, meta, args, rules) -> list:
    """逐张复核"英文文档含中文图"：图没了→gone；仍含中文→still，否则→resolved。已忽略的跳过（也不重跑 OCR）。

    大多数图片自上次 OCR 后并未改动，直接复用已存识别结果（秒级）；只有文件被更新过
    的图才真正重跑 OCR（否则会白跑上千张、把 CPU 跑满还拖垮 Web）。
    """
    stored: dict[str, tuple] = {}
    for img, dj, started in db._conn.execute(
            "SELECT i.item_key, i.detail_json, r.started_at FROM items i "
            "JOIN runs r ON i.run_id=r.id WHERE r.module_key='ocr' ORDER BY i.id"):
        try:
            d = json.loads(dj)
        except Exception:
            continue
        stored[img] = (d.get("ocr_text") or "", d.get("lines") or [],
                       d.get("confidence") or 0, bool(d.get("has_cn")), started)

    def _ts(s) -> float:
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(s)).timestamp()
        except Exception:
            return 0.0

    out, todo, reused = [], [], 0
    for img in sorted(tg["ocr"]):
        parts = img.split("/")
        lang = parts[1] if len(parts) > 2 else ""
        catalog = parts[2] if len(parts) > 3 else ""
        rec = _verdict("ocr", "图片中文", img, None, "", image=img, lang=lang,
                       catalog=catalog, doc_key=img_doc.get(img, ""))
        if ignores.is_ignored(rules, "ocr", img, "has_cn"):
            rec.update(status="ignored", evidence="已忽略")
            out.append(rec)
            continue
        p = BASE_DIR / img
        if not p.exists():
            rec.update(status="gone", evidence="图片文件已删除")
        elif img not in imgs_ref:
            rec.update(status="gone", evidence="图片已不再被任何文档引用")
        else:
            st = stored.get(img)
            if st and st[4] and p.stat().st_mtime <= _ts(st[4]) + 1:
                # 文件自上次识别以来未改动 → 直接复用
                if st[3]:
                    rec.update(status="still", evidence="图片仍含中文（文件未变，复用上次识别）",
                               extra={"ocr_text": st[0], "lines": st[1], "confidence": st[2]})
                else:
                    rec.update(status="resolved", evidence="已无中文（文件未变）")
                reused += 1
            else:
                todo.append(rec)
        out.append(rec)
    if reused:
        print(f"   [ocr] 复用未变动图的识别结果 {reused} 张", flush=True)

    if todo:
        print(f"   [ocr] 需重新识别 {len(todo)} 张图（{args.workers} 进程）", flush=True)
        paths = [BASE_DIR / r["target"] for r in todo]
        batches = [paths[i::args.workers] for i in range(args.workers)]
        got: dict[str, tuple] = {}
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            n = 0
            for res in ex.map(_ocr_worker, [b for b in batches if b]):
                for p, r in res:
                    got[str(p)] = r
                    n += 1
                    if n % 50 == 0:
                        print(f"      {n}/{len(todo)}", flush=True)
        for rec in todo:
            p = rec["target"]
            r = got.get(str(BASE_DIR / p))
            if not r:
                rec.update(status="still", evidence="识别未返回（保持原判）")
                continue
            text, lines, conf = r[0], r[1], r[2]
            has_cn = bool(re.search(r"[\u4e00-\u9fff]", text or ""))
            if has_cn:
                rec.update(status="still", evidence="图片仍含中文",
                           extra={"ocr_text": text, "lines": lines, "confidence": conf})
            else:
                rec.update(status="resolved", evidence="图片已无中文")
    return out


def verdicts_imgnorm(db, tg, imgs_ref, args, rules) -> list:
    """（暂不参与复核）图片内容规范：用已存 OCR 文本跑规则。已忽略的规则类型不计入。"""
    out = []
    ocr_text: dict[str, tuple] = {}
    for item_key, dj in db._conn.execute(
            "SELECT i.item_key, i.detail_json FROM items i JOIN runs r ON i.run_id=r.id "
            "WHERE r.module_key='ocr' ORDER BY i.id"):
        try:
            d = json.loads(dj)
        except Exception:
            continue
        if d.get("image"):
            ocr_text[d["image"]] = (d.get("ocr_text") or "", d.get("lang") or "", d.get("confidence") or 0)
    for img in sorted(tg["imgnorm"]):
        rec = _verdict("imgnorm", "图片规范", img, None, "", image=img)
        p = BASE_DIR / img
        if not p.exists():
            rec.update(status="gone", evidence="图片文件已删除")
            out.append(rec)
            continue
        if img not in imgs_ref:
            rec.update(status="gone", evidence="图片已不再被任何文档引用")
            out.append(rec)
            continue
        text, lang, conf = ocr_text.get(img, ("", "", 0))
        hits, _ev, _x = analyze(text, lang, conf)

        def _ig(field: str) -> bool:
            kind = field[2:] if field.startswith("n_") else field
            return ignores.is_ignored(rules, "imgnorm", img, kind)

        active = {f: n for f, n in hits.items() if not _ig(f)}
        ign = {f: n for f, n in hits.items() if _ig(f)}
        if active:
            labels = [r["label"] for r in RULES if FIELD[r["id"]] in active]
            rec.update(status="still", evidence="、".join(labels)[:80] or "仍有规范问题")
        elif ign:
            labels = [r["label"] for r in RULES if FIELD[r["id"]] in ign]
            rec.update(status="ignored", evidence="已忽略：" + "、".join(labels)[:80])
        else:
            rec.update(status="resolved", evidence="已无规范问题")
        out.append(rec)
    return out


# ───────────────────────────────────────────── 由 verdict 组装"原生形式"item
def build_items(verdicts: list, meta: dict) -> list[tuple]:
    """只保留仍存在(仍存在问题)的项，并还原成各模块原有的 item 结构。"""
    out: list[tuple] = []

    # 链接检查：按文档聚合
    by_doc: dict[str, dict] = {}
    for v in verdicts:
        if v["module"] != "linkcheck" or v["status"] != "still":
            continue
        dk = v["doc_key"]
        b = by_doc.setdefault(dk, {"dead_links": [], "vintage_links": [], "anchor_miss_links": []})
        entry = {"text": v.get("text", "") or v["target"], "url": v["target"]}
        if v["ptype"] == "断链":
            b["dead_links"].append(entry)
        elif v["ptype"] == "误链历史版本":
            b["vintage_links"].append(entry)
        else:
            b["anchor_miss_links"].append(entry)
    for dk, b in by_doc.items():
        url, _ = meta.get(dk, ("", "", "", ""))[0], None
        url = meta.get(dk, ("", "", "", ""))[0] or _doc_key_url(dk)[0]
        lang, catalog = dk.split("|")[:2] if dk.count("|") == 2 else ("", "")
        detail = {
            "module": "linkcheck", "status": "still",
            "doc_key": dk, "lang": lang, "catalog": catalog, "url": url,
            "dead_links": b["dead_links"], "dead_count": len(b["dead_links"]),
            "vintage_links": b["vintage_links"], "vintage_count": len(b["vintage_links"]),
            "anchor_miss_links": b["anchor_miss_links"], "anchor_miss_count": len(b["anchor_miss_links"]),
            "blocked_links": [], "blocked_count": 0,
            "server_links": [], "server_count": 0,
            "unreachable_links": [], "unreachable_count": 0,
        }
        out.append((dk, "still", detail))

    # 英文文档检查：按文档
    for v in verdicts:
        if v["module"] != "encheck" or v["status"] != "still":
            continue
        dk = v["doc_key"]
        res = (v.get("extra") or {}).get("check") or ([], 0, [], 0, [], 0, [], [], 0)
        url = meta.get(dk, ("", "", "", ""))[0] or _doc_key_url(dk)[0]
        title = meta.get(dk, ("", "", "", ""))[1]
        lang, catalog = dk.split("|")[:2] if dk.count("|") == 2 else ("en", "")
        detail = {
            "module": "encheck", "status": "still",
            "doc_key": dk, "lang": lang, "catalog": catalog, "title": title, "doc_url": url,
            "url": url,
            "hanzi": res[0], "hanzi_count": res[1],
            "punct": res[2], "punct_count": res[3],
            "url_cn_chars": res[4], "url_cn_char_count": res[5],
            "url_cn_links": res[6], "cn_links": res[7], "cn_link_count": res[8],
        }
        out.append((dk, "problem", detail))

    # 图片中文检查：按图片
    for v in verdicts:
        if v["module"] != "ocr" or v["status"] != "still":
            continue
        img = v["image"]
        dk = v.get("doc_key", "")
        url = ((meta.get(dk, ("", "", "", ""))[0] or _doc_key_url(dk)[0]) if dk else "")
        ex = v.get("extra") or {}
        detail = {
            "module": "ocr", "status": "still",
            "image": img, "doc_key": dk, "lang": v.get("lang", ""),
            "catalog": v.get("catalog", ""), "doc_url": url,
            "ocr_text": ex.get("ocr_text", ""), "lines": ex.get("lines", []),
            "has_cn": True, "confidence": ex.get("confidence", 0.0),
        }
        out.append((img, "has_cn", detail))

    # imgnorm：暂不参与（如需启用，按同样方式补一段即可）
    return out


# ───────────────────────────────────────────── 主流程
def main():
    ap = argparse.ArgumentParser(description="当前全量问题（历史问题是否已解决）")
    ap.add_argument("--module", default="all", choices=["all"] + list(ALL_MODULES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="每模块最多复核多少项（调试）")
    ap.add_argument("--workers", type=int, default=12, help="OCR 进程数")
    ap.add_argument("--int-delay", type=float, default=0.5, help="站内请求间隔秒")
    ap.add_argument("--timeout", type=float, default=12.0, help="HTTP 超时秒")
    args = ap.parse_args()
    sel = MODULES if args.module == "all" else (args.module,)

    t0 = time.time()
    db = IndexDB(str(BASE_DIR / "index.db"))
    print("📥 收集历史问题目标…", flush=True)
    tg = collect_targets(db)
    print(f"   链接 {len(tg['linkcheck'])} | 英文文档 {len(tg['encheck'])} "
          f"| 图片中文 {len(tg['ocr'])} | 图片规范 {len(tg['imgnorm'])}", flush=True)

    print("🔎 扫描文档引用集（URL / 图片）…", flush=True)
    urls_ref, imgs_ref, img_doc = scan_docs()
    print(f"   引用中 URL {len(urls_ref)} | 图片 {len(imgs_ref)}", flush=True)
    meta = _doc_meta(db)

    if args.limit:
        for k in ("linkcheck", "encheck", "ocr", "imgnorm"):
            if tg[k]:
                items = list(tg[k].items())[:args.limit] if isinstance(tg[k], dict) else list(tg[k])[:args.limit]
                tg[k] = dict(items) if isinstance(tg[k], dict) else set(items)

    rules = db.active_ignore_map()
    n_rules = sum(len(v) for v in rules.values())
    if n_rules:
        print(f"🚫 生效中的忽略 {n_rules} 条（不再复核、不计入解决率）", flush=True)

    verdicts: list = []
    if "linkcheck" in sel and tg["linkcheck"]:
        print(f"🔗 复核链接 {len(tg['linkcheck'])} 项…", flush=True)
        verdicts += verdicts_linkcheck(db, tg, urls_ref, args, meta, rules)
    if "encheck" in sel and tg["encheck"]:
        print(f"🌐 复核英文文档 {len(tg['encheck'])} 篇…", flush=True)
        verdicts += verdicts_encheck(db, tg, meta, args, rules)
    if "ocr" in sel and tg["ocr"]:
        print(f"🔍 复核图片中文 {len(tg['ocr'])} 张…", flush=True)
        verdicts += verdicts_ocr(db, tg, imgs_ref, img_doc, meta, args, rules)
    if "imgnorm" in sel and tg["imgnorm"]:
        print(f"🖼️ 复核图片规范 {len(tg['imgnorm'])} 张…", flush=True)
        verdicts += verdicts_imgnorm(db, tg, imgs_ref, args, rules)

    total = len(verdicts)
    resolved = sum(1 for v in verdicts if v["status"] == "resolved")
    still = sum(1 for v in verdicts if v["status"] == "still")
    gone = sum(1 for v in verdicts if v["status"] == "gone")
    ignored = sum(1 for v in verdicts if v["status"] == "ignored")
    denom = total - ignored      # 已忽略不计入解决率分母（用户口径）
    rate = round(resolved * 100 / denom, 1) if denom else 0.0

    items = build_items(verdicts, meta)
    by_mod: dict[str, dict] = defaultdict(
        lambda: {"resolved": 0, "still": 0, "gone": 0, "ignored": 0})
    item_by_mod: dict[str, int] = defaultdict(int)
    for v in verdicts:
        if v["status"]:
            by_mod[v["module"]][v["status"]] += 1
    for _k, _t, d in items:
        item_by_mod[d["module"]] += 1

    summary = {"total": total, "resolved": resolved, "still": still, "gone": gone,
               "ignored": ignored, "rate": rate,
               # 口径标记：cur=现行窄口径（只统计历史记为问题的链接）；缺省=旧宽口径
               "rate_scope": "cur",
               "elapsed_sec": round(time.time() - t0), "item_count": len(items)}
    for mk in sel:
        summary[f"{mk}_total"] = sum(by_mod[mk].values())
        summary[f"{mk}_resolved"] = by_mod[mk]["resolved"]
        summary[f"{mk}_still"] = by_mod[mk]["still"]
        summary[f"{mk}_gone"] = by_mod[mk]["gone"]
        summary[f"{mk}_ignored"] = by_mod[mk]["ignored"]
        summary[f"{mk}_items"] = item_by_mod[mk]

    print(f"\n📊 复核 {total} 项 | ✅已解决 {resolved} | ⚠️仍存在 {still} | ➖已失效 {gone} "
          f"| 🚫已忽略 {ignored} | 解决率 {rate}%  ({summary['elapsed_sec']}s)", flush=True)
    for mk in sel:
        c = by_mod[mk]
        print(f"   {MODULE_LABEL[mk]:14s} {sum(c.values()):6d}  ✅{c['resolved']:5d} "
              f"⚠️{c['still']:5d} ➖{c['gone']:4d} 🚫{c['ignored']:4d} "
              f"  （明细保留 {item_by_mod[mk]} 项）", flush=True)

    if args.dry_run:
        db.close()
        print("   [dry-run] 未写库", flush=True)
        return

    run_id = db.start_run("recheck")
    for k, itype, detail in items:
        db.add_item(run_id, k, itype, detail)
    db.finish_run(run_id, summary)
    db.close()
    print(f"✅ 复核完成: run#{run_id}", flush=True)


if __name__ == "__main__":
    main()
