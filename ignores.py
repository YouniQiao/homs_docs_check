"""忽略：把「某些检测结果」标为不看（永久，手动恢复）。

设计（2026-09 与用户确认）：
- 粒度 = (模块, 目标, 问题类型)，类型可多选 → 既能"只忽略某一类问题"，也能全选=整条忽略。
    · linkcheck：目标 = 链接 URL（默认全局：任何文档里出现都不再报；可勾"仅本文档"）
    · encheck：  目标 = doc_key
    · ocr / imgnorm：目标 = 图片相对路径
- **只做展示/统计端过滤**：run 数据照常写入 → 恢复即时生效（不必重跑），且数据库保留原始检测结果。
- 恢复不物理删记录（`restored_at` 标记），保留"曾经忽略过什么、谁恢复的"。
- 无鉴权（用户 2026-09 决定）：站点公开可写；用 created_ip / restored_ip 留痕。
"""

from __future__ import annotations

# kind 定义：(kind, 标签, detail 里的计数字段, detail 里的明细列表字段)
KINDS: dict[str, list[tuple]] = {
    "linkcheck": [("dead", "断链", "dead_count", "dead_links"),
                  ("vintage", "误链历史版本", "vintage_count", "vintage_links"),
                  ("anchor_miss", "锚点失效", "anchor_miss_count", "anchor_miss_links")],
    "encheck": [("hanzi", "含汉字", "hanzi_count", None),
                ("punct", "含标点", "punct_count", None),
                ("url_cn", "链接URL含中文", "url_cn_char_count", "url_cn_links"),
                ("cn_link", "含中文链接", "cn_link_count", "cn_links")],
    "ocr": [("has_cn", "图片含中文", None, None)],
    "imgnorm": [],
}


def _load_imgnorm_kinds() -> list[tuple]:
    try:
        from imgnorm.rules import FIELD, RULES
    except Exception:
        return []
    return [(r["id"], r["label"], FIELD[r["id"]], None) for r in RULES]


KINDS["imgnorm"] = _load_imgnorm_kinds()


def _load_sysmerge_kinds() -> list[tuple]:
    """系统合并整改：匹配词本身就是「问题类型」（词表可编辑，故动态加载）。"""
    try:
        import json as _json
        import pathlib as _pl
        p = _pl.Path(__file__).resolve().parent / "sysmerge" / "keywords.json"
        d = _json.loads(p.read_text(encoding="utf-8"))
        return [(t, t, None, None) for t in (d.get("terms", []) + d.get("links", []))]
    except Exception:
        return []


KINDS["sysmerge"] = _load_sysmerge_kinds()

# 目标在界面上的称呼
TARGET_LABEL = {"linkcheck": "链接", "encheck": "文档", "ocr": "图片",
                "imgnorm": "图片", "sysmerge": "文档"}

# 模块在本站的显示名（用于忽略记录/管理展示）
MODULE_LABEL = {"linkcheck": "链接健康检查", "encheck": "英文文档检查",
                "ocr": "图片 OCR 检查", "imgnorm": "图片内容规范检查",
                "sync": "文档同步", "recheck": "当前全量问题",
                "sysmerge": "系统合并整改"}


def supports(module_key: str) -> bool:
    """该模块是否支持忽略。"""
    return bool(KINDS.get(module_key))


# ── 忽略记录的后端：默认写 index.db 的 ignores 表；模块可注册自己的后端 ──
# 用途：系统合并整改（sysmerge）要求忽略记录放独立库（sysmerge/ignore.db），
# 通过 register_backend("sysmerge", lambda: IgnoreStore()) 接入，其余模块不受影响。
_BACKENDS: dict = {}


def register_backend(module_key: str, factory) -> None:
    """factory() -> 具有 active_ignore_map/add_ignore/restore_ignores_for 的对象。"""
    _BACKENDS[module_key] = factory


def _backend(db, module_key: str):
    f = _BACKENDS.get(module_key)
    if f is None:
        return db
    try:
        return f()
    except Exception:
        return db


def kinds_of(module_key: str) -> list[tuple]:
    return KINDS.get(module_key, [])


def kind_label(module_key: str, kind: str) -> str:
    for k, label, _f, _l in KINDS.get(module_key, ()):
        if k == kind:
            return label
    return kind


def active_map(db) -> dict:
    """生效中的忽略 → {module: [{target,doc_key,kind}]}（便捷包装）。

    含已注册独立后端（如 sysmerge）的记录，其余模块仍取 index.db。
    """
    m = db.active_ignore_map()
    for mk, f in _BACKENDS.items():
        try:
            m[mk] = f().active_ignore_map().get(mk, [])
        except Exception:
            m.setdefault(mk, [])
    return m


def is_ignored(rules: dict, module_key: str, target: str, kind: str,
               doc_key: str = "") -> bool:
    """rules 为 db.active_ignore_map() 的返回值：{module: [{target,doc_key,kind}]}。

    记录 doc_key 非空 = "仅本文档"忽略；调用方不给 doc_key 时不匹配（保守）。
    """
    if not target:
        return False
    for r in rules.get(module_key, ()):
        if r["kind"] != kind:
            continue
        if r["target"] not in ("*", target):   # "*" = 整词忽略（该匹配词全忽略）
            continue
        if r["doc_key"] and r["doc_key"] != doc_key:
            continue
        return True
    return False


def item_problems(module_key: str, detail: dict, item_type: str = "") -> list[dict]:
    """列举一条 item 上的每个问题：{kind, kind_label, target, doc_key, label}。

    顺序必须稳定（前端复选框按序号提交，后端据此重算）。
    """
    d = detail or {}
    out: list[dict] = []
    if module_key == "linkcheck":
        dk = d.get("doc_key", "") or ""
        for kind, label, _cnt, list_f in KINDS["linkcheck"]:
            for l in (d.get(list_f) or []):
                url = l.get("url") if isinstance(l, dict) else l
                if not url:
                    continue
                text = l.get("text", "") if isinstance(l, dict) else ""
                out.append({"kind": kind, "kind_label": label, "target": url,
                            "doc_key": dk, "label": text or url, "inline": True})
    elif module_key == "encheck":
        dk = d.get("doc_key", "") or ""
        for kind, label, cnt_f, list_f in KINDS["encheck"]:
            if kind == "cn_link":
                # 中文链接：**逐条链接**（目标 = 链接 URL，与链接检查同粒度）——
                # 用户在页面上点的是某一条链接旁的开关，忽略记录也应是那条链接。
                for l in (d.get("cn_links") or []):
                    url = l.get("url") if isinstance(l, dict) else l
                    if not url:
                        continue
                    text = l.get("text", "") if isinstance(l, dict) else ""
                    out.append({"kind": kind, "kind_label": label, "target": url,
                                "doc_key": "", "label": text or url, "inline": True})
            elif d.get(cnt_f, 0) > 0:
                # 含汉字 / 含标点 / 链接URL含中文：文档级（目标 = 文档）
                out.append({"kind": kind, "kind_label": label, "target": dk,
                            "doc_key": "", "label": "本文档"})
    elif module_key == "ocr":
        img = d.get("image", "") or ""
        if img and (item_type == "has_cn" or d.get("has_cn")):
            out.append({"kind": "has_cn", "kind_label": "图片含中文", "target": img,
                        "doc_key": "", "label": img})
    elif module_key == "imgnorm":
        img = d.get("image", "") or ""
        for kind, label, cnt_f, _l in KINDS["imgnorm"]:
            if d.get(cnt_f, 0) > 0:
                out.append({"kind": kind, "kind_label": label, "target": img,
                            "doc_key": "", "label": label})
    elif module_key == "sysmerge":
        # 一条 item = 一篇文档 × 一个匹配词；目标 = 文档 doc_key，kind = 匹配词
        term = d.get("matched", "") or ""
        dk = d.get("doc_key", "") or ""
        if term:
            # 链接类匹配词很长（老文档 URL），chip 上只显示尾部，完整值放 title
            label = term if len(term) <= 30 else "…" + term[-28:]
            out.append({"kind": term, "kind_label": term, "target": dk,
                        "doc_key": "", "label": label, "full": term})
    return out


def strip(module_key: str, detail: dict, rules: dict,
          item_type: str = "") -> tuple[dict, int, int]:
    """裁掉被忽略的问题，返回 (新 detail, 被忽略的问题数, 剩余问题数)。

    不就地修改原 detail（展示端用它重算计数/徽标，run 里的原始数据不受影响）。
    """
    d = dict(detail or {})
    ign = rem = 0
    if module_key == "linkcheck":
        dk = d.get("doc_key", "") or ""
        for kind, _label, cnt_f, list_f in KINDS["linkcheck"]:
            kept = []
            for l in (d.get(list_f) or []):
                url = l.get("url") if isinstance(l, dict) else l
                if url and is_ignored(rules, module_key, url, kind, dk):
                    ign += 1
                else:
                    kept.append(l)
                    rem += 1
            d[list_f] = kept
            d[cnt_f] = len(kept)
    elif module_key == "encheck":
        dk = d.get("doc_key", "") or ""
        for kind, _label, cnt_f, list_f in KINDS["encheck"]:
            if kind == "cn_link":
                # 逐条链接：过滤掉被忽略的链接，计数 = 剩余链接数
                kept = []
                for l in (d.get("cn_links") or []):
                    url = l.get("url") if isinstance(l, dict) else l
                    if url and is_ignored(rules, module_key, url, kind):
                        ign += 1
                    else:
                        kept.append(l)
                        rem += 1
                d["cn_links"] = kept
                d["cn_link_count"] = len(kept)
            elif d.get(cnt_f, 0) > 0:
                if is_ignored(rules, module_key, dk, kind):
                    ign += 1
                    d[cnt_f] = 0
                    if list_f:
                        d[list_f] = []
                else:
                    rem += 1
    elif module_key == "ocr":
        img = d.get("image", "") or ""
        if img and (item_type == "has_cn" or d.get("has_cn")):
            if is_ignored(rules, module_key, img, "has_cn"):
                ign += 1
                d["has_cn"] = False
            else:
                rem += 1
    elif module_key == "imgnorm":
        img = d.get("image", "") or ""
        for kind, _label, cnt_f, _l in KINDS["imgnorm"]:
            if d.get(cnt_f, 0) > 0:
                if is_ignored(rules, module_key, img, kind):
                    ign += 1
                    d[cnt_f] = 0
                else:
                    rem += 1
    elif module_key == "sysmerge":
        term = d.get("matched", "") or ""
        dk = d.get("doc_key", "") or ""
        if term:
            if is_ignored(rules, module_key, dk, term):
                ign += 1
                d["ignored"] = True
            else:
                rem += 1
    return d, ign, rem


def encode_problem(p: dict) -> str:
    """把一个问题编码成忽略记录 (target, doc_key, kind)。"""
    return f"{p['kind']}\x1f{p['target']}\x1f{p.get('doc_key', '')}"


def decode_problem(s: str) -> tuple[str, str, str]:
    parts = (s or "").split("\x1f")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def apply_selection(db, module_key: str, problems: list[dict], selected: set[str],
                    reason: str = "", ip: str = "") -> tuple[int, int]:
    """按期望状态同步忽略：在 selected 里的建忽略、不在的恢复。

    selected 是 encode_problem 的字符串集合。返回 (新增数, 恢复数)。
    """
    rules = active_map(db)
    added = restored = 0
    for p in problems:
        key = encode_problem(p)
        want = key in selected
        have = is_ignored(rules, module_key, p["target"], p["kind"], p.get("doc_key", ""))
        if want and not have:
            if _backend(db, module_key).add_ignore(
                    module_key, p["target"], p["kind"], doc_key="",
                    reason=reason, ip=ip):
                added += 1
                rules.setdefault(module_key, []).append(
                    {"target": p["target"], "doc_key": "", "kind": p["kind"]})
        elif (not want) and have:
            n = _backend(db, module_key).restore_ignores_for(
                module_key, p["target"], p["kind"], p.get("doc_key", ""), ip=ip)
            restored += n
            if n:
                rules[module_key] = [
                    r for r in rules.get(module_key, [])
                    if not (r["target"] == p["target"] and r["kind"] == p["kind"]
                            and (not r["doc_key"] or r["doc_key"] == p.get("doc_key", "")))]
    return added, restored
