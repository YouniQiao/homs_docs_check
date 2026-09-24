"""「我的」→ 关注领域配置（P2a 阶段）。

口径（用户 2026-09 拍板）：关注领域**只允许 4 个维度**
  catalog  文档类型 —— 固定 5 个取值
  kit      Kit      —— 从 docs 表 DISTINCT kit（按文档数倒序）
  ide      IDE 分组 —— 从 docs 表 DISTINCT ide（按文档数倒序）
  module   检查模块 —— 本站固定 4 个：ocr / encheck / linkcheck / sysmerge

已选值落在 index.db 的 user_areas 表（按用户，UNIQUE(user_id, dim, value)）；
POST /me/areas 增删，保存即生效，无前端框架（每个标签/下拉一个原生 form）。

口径预览（本期新增）：「关注领域」下方并列展示两种统计口径的命中文档数——
  甲 union        并集：命中任一已选维度（OR）
  乙 intersection 交集：同时命中所有已选维度（AND，只对已选维度取交集）
口径开关存 user_prefs(user_id PK, area_logic, updated_at)，只存不算；本阶段**只做预览**，
「我的问题列表」在 P2b。module 不是 docs 的列，无法按 doc_key 判定，不参与文档数。

注意：忽略与「已处理」都是**全局**的，不按用户区分（用户 2026-09 拍板）。

「我的问题」（P2b，本文件）——两段：
  ① 每日增量：各模块**最新一次成功 run** 的条目（日常 run 是增量的），落在关注领域
     （并集口径）内的「问题条目」，按模块分组、可折叠；每条给「忽略 / 已处理」两个操作。
  ② 全量：**跨 run 按 item_key 去重后仍存在**的条目——优先取「当前全量问题」
     （module_key=recheck）最新一次成功 run 里该模块的条目（recheck 把历史问题跨 run
     去重后逐条复核，只写「仍存在」的）；recheck 不覆盖的模块（sysmerge）退回该模块
     最新一次成功 run 的条目（整站全量扫描，天然是「当前全量」）。默认折叠。
忽略 / 已处理都是**展示端过滤**：run/items 数据一律不动（恢复即时生效），
被忽略或被处理完的条目不计入「仍存在」，并单列计数 + 可展开列表（可恢复/撤销）。

路由：
  POST /me/areas   action=add|remove  + dim + value → 302 回 /me（flash 提示）
  POST /me/logic   logic=union|intersection        → 302 回 /me（口径开关，只存不算）
  POST /me/issue   run_id + item_id + act=ignore|unignore|handle|unhandle → 302 回 /me
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Blueprint, flash, redirect, request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ignores  # noqa: E402
from db import IndexDB  # noqa: E402

DB_PATH = str(BASE_DIR / "index.db")

# 4 个维度（顺序即页面展示顺序）
DIMS = ("catalog", "kit", "ide", "module")
DIM_LABELS = {
    "catalog": "文档类型",
    "kit": "Kit",
    "ide": "IDE 分组",
    "module": "检查模块",
}
DIM_HINTS = {
    "catalog": "按文档类型关注（指南 / API 参考 / FAQ / 版本说明 / 最佳实践）",
    "kit": "按 Kit 关注（如 ArkUI、Ability Kit）；kit 只覆盖 harmonyos-guides 与 harmonyos-references",
    "ide": "按 IDE 分组关注（如「编写与调试应用」「开发环境搭建」）",
    "module": "按检查模块关注：图片 OCR / 英文文档 / 链接健康 / 系统词合并",
}

# catalog 固定 5 值（与 docs.catalog 的实际取值一致）
CATALOG_LABELS = {
    "harmonyos-guides": "指南（harmonyos-guides）",
    "harmonyos-references": "API 参考（harmonyos-references）",
    "harmonyos-faqs": "FAQ（harmonyos-faqs）",
    "harmonyos-releases": "版本说明（harmonyos-releases）",
    "best-practices": "最佳实践（best-practices）",
}

# module 建议取值（本站 4 个检查模块）
MODULE_LABELS = {
    "ocr": "图片 OCR 检查（ocr）",
    "encheck": "英文文档检查（encheck）",
    "linkcheck": "链接健康检查（linkcheck）",
    "sysmerge": "系统词合并检查（sysmerge）",
}

MAX_VALUE_LEN = 120

# ── 口径预览（两种理解并列展示；本阶段只预览 + 存开关，不做问题列表）──────
# 只有 catalog / kit / ide 是 docs 表的列，能按 doc_key 判定命中；
# module（检查模块）不是文档属性，无法落到 doc_key 上，故不参与两组数字。
DOC_DIMS = ("catalog", "kit", "ide")
AREA_LOGICS = ("union", "intersection")
LOGIC_LABELS = {"union": "甲 · 并集", "intersection": "乙 · 交集"}
LOGIC_SHORT = {"union": "并集", "intersection": "交集"}
LOGIC_DESC = (
    "两种口径的差别：甲（并集）把「命中任一已选维度」的文档都算成你的，范围宽；"
    "乙（交集）只算「同时命中所有已选维度」的文档，范围窄——Kit 与 IDE 分组几乎不重叠，"
    "所以乙常常是空集。未选的维度不参与口径（否则交集恒为空）。"
    "检查模块不是文档属性（无法按 doc_key 判定），不参与这里的文档数。"
)
_MAX_SAMPLE = 3

me_bp = Blueprint("me_pages", __name__)


# ── 选项/已选（供 /me 页面渲染）────────────────────────────────────────
def _docs_counts(db, col: str) -> list[tuple[str, int]]:
    """docs 表某列的非空取值 → [(value, 文档数)]，按文档数倒序（列名白名单，防注入）。"""
    if col not in ("catalog", "kit", "ide"):
        return []
    sql = (f"SELECT {col} AS v, COUNT(*) AS c FROM docs"
           f" WHERE {col} IS NOT NULL AND {col}<>''"
           f" GROUP BY {col} ORDER BY c DESC, v ASC")
    return [(r[0], int(r[1])) for r in db._conn.execute(sql)]


def area_options(db) -> dict:
    """{dim: [(value, label, count)]}；kit/ide 按文档数倒序，catalog/module 固定顺序。"""
    catalog_counts = dict(_docs_counts(db, "catalog"))
    out: dict = {
        "catalog": [(v, CATALOG_LABELS[v], catalog_counts.get(v, 0))
                    for v in CATALOG_LABELS],
        "kit": [(v, v, c) for v, c in _docs_counts(db, "kit")],
        "ide": [(v, v, c) for v, c in _docs_counts(db, "ide")],
        "module": [(v, MODULE_LABELS[v], 0) for v in MODULE_LABELS],
    }
    return out


def _label_map(options: dict) -> dict:
    return {dim: {v: lbl for v, lbl, _c in opts} for dim, opts in (options or {}).items()}


def selected_areas(db, user_id: int) -> dict:
    """{dim: [value, ...]}（含已不在当前选项里的历史值，保证标签可删）。"""
    out: dict = {d: [] for d in DIMS}
    for r in db.list_user_areas(user_id):
        out.setdefault(r["dim"], []).append(r["value"])
    return out


# ── 口径预览：按已选关注领域算两种口径命中的文档（只读 docs 表，不写数据）──
def _sel_doc_dims(areas: dict) -> list[tuple[str, list[str]]]:
    """[(dim, [value, ...])]，只保留有值的文档维度（catalog / kit / ide）。"""
    out: list[tuple[str, list[str]]] = []
    for dim in DOC_DIMS:
        vals = [v for v in ((areas or {}).get(dim) or []) if v]
        if vals:
            out.append((dim, vals))
    return out


def doc_logic_preview(db, areas: dict, sample_n: int = _MAX_SAMPLE) -> dict:
    """两种口径的预览数据：文档数 + 各 2-3 个示例文档。

    甲 union        = 命中任一已选维度（OR）——「范围宽」
    乙 intersection = 同时命中所有已选维度（AND；只对**已选**维度取交集，
                      未选维度不参与，否则交集恒为空）
    未选任何文档维度时按「默认全部」展示全站文档数（甲=乙=全部）。
    说明：module 不是 docs 的列，无法按 doc_key 判定，故不计入文档数。
    """
    sel = _sel_doc_dims(areas)
    total = int(db._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
    out: dict = {
        "has_doc_dims": bool(sel),
        "selected": {dim: vals for dim, vals in sel},
        "modules": [v for v in ((areas or {}).get("module") or []) if v],
        "total_docs": total,
        "total_docs_fmt": f"{total:,}",
        "union": {"count": 0, "count_fmt": "0", "samples": []},
        "intersection": {"count": 0, "count_fmt": "0", "samples": []},
        "empty_intersection": False,
    }
    if not sel:
        # 未设关注领域 → 默认全部（两种口径一致，示例取全站前几篇）
        rows = db._conn.execute(
            "SELECT doc_key, title, catalog, kit, ide FROM docs"
            " ORDER BY catalog, kit, title LIMIT ?", (sample_n,)).fetchall()
        samples = [{"doc_key": r[0], "title": r[1] or r[0], "catalog": r[2],
                    "kit": r[3] or "", "ide": r[4] or ""} for r in rows]
        for k in ("union", "intersection"):
            out[k]["count"] = total
            out[k]["count_fmt"] = out["total_docs_fmt"]
            out[k]["samples"] = samples
        return out

    for logic, op in (("union", " OR "), ("intersection", " AND ")):
        where = op.join(
            f"({dim} IN ({','.join('?' * len(vals))}))" for dim, vals in sel)
        params: list = [v for _dim, vals in sel for v in vals]
        n = int(db._conn.execute(
            f"SELECT COUNT(*) FROM docs WHERE {where}", params).fetchone()[0])
        rows = db._conn.execute(
            f"SELECT doc_key, title, catalog, kit, ide FROM docs WHERE {where}"
            f" ORDER BY catalog, kit, title LIMIT ?", params + [sample_n]).fetchall()
        out[logic] = {
            "count": n,
            "count_fmt": f"{n:,}",
            "samples": [{"doc_key": r[0], "title": r[1] or r[0], "catalog": r[2],
                         "kit": r[3] or "", "ide": r[4] or ""} for r in rows],
        }
    out["empty_intersection"] = out["intersection"]["count"] == 0
    return out


# ── 我的问题（P2b）──────────────────────────────────────────────────────
# 4 个检查模块（module 维度只作用于问题所属模块；kit/ide 只对 guides/references 有意义）
ISSUE_MODULES = ("ocr", "encheck", "linkcheck", "sysmerge")
ISSUE_LABELS = {"ocr": "图片 OCR 检查", "encheck": "英文文档检查",
                "linkcheck": "链接健康检查", "sysmerge": "系统合并整改"}
ISSUE_ICONS = {"ocr": "🔍", "encheck": "🌐", "linkcheck": "🔗", "sysmerge": "🧩"}
# 「当前全量问题」（recheck）覆盖的模块；其余（sysmerge）取自身最新一次成功 run
RECHECK_MODULES = ("ocr", "encheck", "linkcheck")
ISSUE_MAX_ITEMS = 30          # 每组最多渲染多少条（其余给「还有 N 条」+ 模块页链接）
ISSUE_HINT = ("「仍存在」= 最新一次检查里还有、且没被忽略/处理掉的条目；"
              "已忽略 / 已处理都是全局生效（谁先点谁生效，不按用户区分），"
              "被处理完的条目不计入「仍存在」但保留可见，可随时恢复。")


def _fmt_time(s) -> str:
    """ISO 时间（2026-09-23T05:10:01）→ 2026-09-23 05:10:01（空值返回空串）。"""
    if not s:
        return ""
    return str(s).replace("T", " ")[:19]


def _client_ip() -> str:
    """记录忽略/已处理操作的来源 IP（无鉴权，留痕便于事后追溯）。"""
    try:
        x = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "")
        return (x.split(",")[0].strip() or request.remote_addr or "")[:64]
    except Exception:  # noqa: BLE001 - 取不到 IP 不该影响主流程
        return ""


def _latest_success_run(db, module_key: str) -> dict | None:
    """某模块最新一次成功 run（日常 run 是增量的，故「每日增量」= 这一条）。"""
    row = db._conn.execute(
        "SELECT id, started_at, finished_at, summary_json FROM runs"
        " WHERE module_key=? AND status='success' ORDER BY id DESC LIMIT 1",
        (module_key,)).fetchone()
    if not row:
        return None
    try:
        summary = json.loads(row[3] or "{}")
    except Exception:  # noqa: BLE001 - 摘要坏了不影响条目列表
        summary = {}
    return {"id": row[0], "module_key": module_key, "started_at": row[1],
            "finished_at": row[2], "summary": summary}


def _run_items(db, run_id: int) -> list[dict]:
    """某次 run 的条目；同一 item_key 只保留最后一条（= 跨 run/run 内去重口径）。"""
    out: dict = {}
    for iid, key, itype, dj in db._conn.execute(
            "SELECT id, item_key, item_type, detail_json FROM items"
            " WHERE run_id=? ORDER BY id", (run_id,)):
        try:
            detail = json.loads(dj or "{}")
        except Exception:  # noqa: BLE001
            detail = {}
        out[key or f"#{iid}"] = {"id": iid, "item_key": key, "item_type": itype,
                                 "detail": detail}
    return list(out.values())


def _docs_meta(db, keys) -> dict:
    """doc_key → {title, catalog, kit, ide, url}（分批 IN，避免超长 SQL）。"""
    keys = sorted({k for k in keys if k})
    out: dict = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        sql = ("SELECT doc_key, title, catalog, kit, ide, url FROM docs"
               " WHERE doc_key IN (%s)" % ",".join("?" * len(chunk)))
        for dk, title, cat, kit, ide, url in db._conn.execute(sql, chunk):
            out[dk] = {"title": title or "", "catalog": cat or "", "kit": kit or "",
                       "ide": ide or "", "url": url or ""}
    return out


def _area_selection(areas: dict) -> tuple[list, list, list, list]:
    """已选关注领域 → (catalog, kit, ide, module) 四组取值（去掉空值）。"""
    def g(dim: str) -> list:
        return [v for v in ((areas or {}).get(dim) or []) if v]

    return g("catalog"), g("kit"), g("ide"), g("module")


def _doc_hit(meta: dict, detail: dict, cats: list, kits: list, ides: list) -> bool:
    """并集口径：命中任一已选文档维度即算「我的」；三个维度都没选 = 不限制（默认全部）。

    kit / ide 只有 harmonyos-guides 与 harmonyos-references 有值（faqs / releases /
    best-practices 为空），那三类文档只能靠 catalog 命中——与口径预览一致。
    """
    if not (cats or kits or ides):
        return True
    cat = (meta or {}).get("catalog") or (detail or {}).get("catalog") or ""
    kit = (meta or {}).get("kit") or ""
    ide = (meta or {}).get("ide") or ""
    return bool((cats and cat in cats) or (kits and kit in kits) or (ides and ide in ides))


def _problem_state(mk: str, detail: dict, item_type: str, rules: dict,
                   handled_rules: dict) -> tuple[list, dict]:
    """条目内每个问题的状态 → (problems, 状态汇总)。

    第 4 个状态「已处理」与「已忽略」并列；优先级：忽略 > 已处理 > 仍存在。
    """
    probs = ignores.item_problems(mk, detail, item_type)
    n_ign = n_hand = 0
    for p in probs:
        p["ignored"] = ignores.is_ignored(rules, mk, p["target"], p["kind"],
                                          p.get("doc_key", ""))
        p["handled"] = (not p["ignored"]) and ignores.is_handled(
            handled_rules, mk, p["target"], p["kind"], p.get("doc_key", ""))
        n_ign += 1 if p["ignored"] else 0
        n_hand += 1 if p["handled"] else 0
    n_open = len(probs) - n_ign - n_hand
    st = "open" if n_open else ("handled" if n_hand else ("ignored" if n_ign else "none"))
    return probs, {"status": st, "n_probs": len(probs), "n_open": n_open,
                   "n_ignored": n_ign, "n_handled": n_hand,
                   "all_ignored": bool(probs) and n_ign == len(probs),
                   "all_handled": bool(probs) and n_hand == len(probs)}


def _is_problem_item(mk: str, item_type: str, detail: dict, probs: list) -> bool:
    """「问题条目」判定：有可忽略的问题，或本身是识别/读取失败（ocr / encheck）。

    其余条目（如 ocr 的 no_cn、encheck 的 clean、linkcheck 的 blocked/server）不是问题，
    只在分组头部计入「正常」数，不列进问题列表。
    """
    if probs:
        return True
    return mk in ("ocr", "encheck") and item_type == "error"


def _item_summary(mk: str, detail: dict, item_type: str, probs: list) -> tuple[str, str]:
    """(问题摘要, 补充信息)：摘要按问题类型聚合计数，补充信息给最有用的一行上下文。"""
    if not probs:
        if item_type == "error":
            return ("识别失败" if mk == "ocr" else "读取失败"), str(
                detail.get("error") or "")[:160]
        return "正常", ""
    counts: dict = {}
    for p in probs:
        counts[p["kind_label"]] = counts.get(p["kind_label"], 0) + 1
    summary = "、".join(f"{lab} × {n}" if n > 1 else lab for lab, n in counts.items())

    extra = ""
    if mk == "sysmerge":
        extra = (f"命中「{detail.get('matched', '')}」"
                 f"（{detail.get('matched_kind', '')}，共 {detail.get('count', 1)} 次）")
        if detail.get("snippet"):
            extra += " · " + str(detail["snippet"])
    elif mk == "linkcheck":
        urls = []
        for f in ("dead_links", "vintage_links", "anchor_miss_links"):
            for l in (detail.get(f) or [])[:1]:
                u = l.get("url") if isinstance(l, dict) else l
                if u:
                    urls.append(str(u))
        extra = " · ".join(urls[:2])
    elif mk == "encheck":
        vals = []
        for f, lab in (("hanzi", "汉字"), ("punct", "标点")):
            v = detail.get(f)
            if v:
                vals.append(f"{lab}：{' '.join(str(x) for x in v[:12])}"
                            if isinstance(v, list) else f"{lab}：{v}")
        extra = " · ".join(vals)
    elif mk == "ocr":
        extra = str(detail.get("ocr_text") or "")
    return summary, extra[:160]


def _build_issue_group(db, mk: str, run: dict | None, raw_items: list, metas: dict,
                       cats: list, kits: list, ides: list, rules: dict,
                       handled_rules: dict, source: str = "") -> dict:
    """把一次 run 的条目整理成一个模块分组（含三态计数 + 三条列表）。"""
    g = {"module": mk, "label": ISSUE_LABELS.get(mk, mk), "icon": ISSUE_ICONS.get(mk, ""),
         "source": source, "run_id": run["id"] if run else None,
         "run_at": _fmt_time((run or {}).get("started_at")),
         "checked": len(raw_items), "n_total": 0, "n_open": 0, "n_ignored": 0,
         "n_handled": 0, "n_out": 0, "n_normal": 0,
         "open": [], "ignored": [], "handled": []}
    for it in raw_items:
        d = it["detail"] or {}
        dk = d.get("doc_key") or it["item_key"] or ""
        meta = metas.get(dk) or {}
        if not _doc_hit(meta, d, cats, kits, ides):
            g["n_out"] += 1
            continue
        probs, st = _problem_state(mk, d, it["item_type"], rules, handled_rules)
        if not _is_problem_item(mk, it["item_type"], d, probs):
            g["n_normal"] += 1
            continue
        summary, extra = _item_summary(mk, d, it["item_type"], probs)
        row = {
            "id": it["id"], "run_id": g["run_id"], "module": mk,
            "module_label": g["label"],
            "title": (meta.get("title") or d.get("doc_title") or d.get("title")
                      or dk or it["item_key"] or ""),
            "doc_key": dk, "catalog": meta.get("catalog") or d.get("catalog") or "",
            "kit": meta.get("kit") or d.get("kit") or "", "ide": meta.get("ide") or "",
            "lang": d.get("lang") or "", "item_type": it["item_type"],
            "summary": summary, "extra": extra, "status": st["status"],
            "n_probs": st["n_probs"], "n_open": st["n_open"],
            "all_ignored": st["all_ignored"], "all_handled": st["all_handled"],
            "url": meta.get("url") or d.get("doc_url") or d.get("url") or "",
            "can_act": bool(probs),
        }
        g["n_total"] += 1
        bucket = st["status"] if st["status"] in ("open", "ignored", "handled") else "open"
        g["n_" + bucket] += 1
        g[bucket].append(row)
    for key in ("open", "ignored", "handled"):
        g[key + "_more"] = max(0, len(g[key]) - ISSUE_MAX_ITEMS)
        g[key] = g[key][:ISSUE_MAX_ITEMS]
    return g


def _totals(groups: list) -> dict:
    keys = ("n_total", "n_open", "n_ignored", "n_handled", "n_out", "n_normal")
    return {k: sum(g[k] for g in groups) for k in keys}


def issue_context(db, areas: dict) -> dict:
    """「📋 我的问题」两段数据（每日增量 / 全量）+ 已处理汇总；异常时降级为空。"""
    cats, kits, ides, mods = _area_selection(areas)
    out = {
        "has_areas": bool(cats or kits or ides or mods),
        "issue_modules": ISSUE_MODULES, "issue_labels": ISSUE_LABELS,
        "issue_icons": ISSUE_ICONS, "issue_max": ISSUE_MAX_ITEMS, "issue_hint": ISSUE_HINT,
        "daily": [], "full": [], "daily_totals": {}, "full_totals": {},
        "handled_rows": [], "handled_total": 0, "recheck_run": None,
    }
    try:
        rules = ignores.active_map(db)
        handled_rules = db.active_handled_map()
    except Exception:  # noqa: BLE001 - 取不到忽略/已处理时按「没有」处理（页面照常出）
        rules, handled_rules = {}, {}

    # ① 每日增量：各模块最新一次成功 run
    daily_runs: dict = {}
    for mk in ISSUE_MODULES:
        if mods and mk not in mods:
            continue
        run = _latest_success_run(db, mk)
        daily_runs[mk] = (run, _run_items(db, run["id"]) if run else [])

    # ② 全量：recheck 最新一次成功 run（跨 run 去重后仍存在）+ sysmerge 自身最新 run
    rc = _latest_success_run(db, "recheck")
    out["recheck_run"] = {"id": rc["id"], "run_at": _fmt_time(rc["started_at"])} if rc else None
    rc_by_mod: dict = {}
    if rc:
        for it in _run_items(db, rc["id"]):
            mk = (it["detail"] or {}).get("module") or ""
            if mk:
                rc_by_mod.setdefault(mk, []).append(it)
    full_runs: dict = {}
    for mk in ISSUE_MODULES:
        if mods and mk not in mods:
            continue
        if mk in RECHECK_MODULES:
            src = (f"🗓️ 当前全量问题 #{rc['id']}（跨 run 去重后仍存在）" if rc else "")
            full_runs[mk] = (rc, rc_by_mod.get(mk, []) if rc else [], src)
        else:
            run = _latest_success_run(db, mk)
            full_runs[mk] = (run, _run_items(db, run["id"]) if run else [],
                             f"🧩 {ISSUE_LABELS[mk]} #{run['id']}（整站全量扫描）" if run else "")

    # 文档元信息一次批量取（两个区共用）
    keys: list = []
    for run, items in daily_runs.values():
        keys += [(it["detail"] or {}).get("doc_key") or it["item_key"] for it in items]
    for run, items, _s in full_runs.values():
        keys += [(it["detail"] or {}).get("doc_key") or it["item_key"] for it in items]
    metas = _docs_meta(db, keys)

    out["daily"] = [_build_issue_group(db, mk, run, items, metas, cats, kits, ides,
                                       rules, handled_rules)
                    for mk, (run, items) in daily_runs.items()]
    out["full"] = [_build_issue_group(db, mk, run, items, metas, cats, kits, ides,
                                      rules, handled_rules, source=src)
                   for mk, (run, items, src) in full_runs.items()]
    out["daily_totals"] = _totals(out["daily"])
    out["full_totals"] = _totals(out["full"])

    # 已处理：单列计数（全局生效的 handled 记录，含 sysmerge 等所有模块）
    try:
        hrows = db.list_handled(active_only=True)
    except Exception:  # noqa: BLE001
        hrows = []
    out["handled_rows"] = [{"module_label": ISSUE_LABELS.get(r["module_key"],
                                                            ignores.MODULE_LABEL.get(r["module_key"], r["module_key"])),
                            "kind_label": ignores.kind_label(r["module_key"], r["kind"]),
                            "target": r["target"], "created_at": r["created_at"],
                            "created_by": r["created_by"], "id": r["id"]}
                           for r in hrows]
    out["handled_total"] = len(hrows)
    return out


def _empty_context() -> dict:
    return {"areas": {d: [] for d in DIMS}, "area_options": {}, "labels": {},
            "dims": DIMS, "dim_labels": DIM_LABELS, "dim_hints": DIM_HINTS,
            "area_total": 0, "area_logic": "union", "logic_labels": LOGIC_LABELS,
            "logic_short": LOGIC_SHORT, "logic_desc": LOGIC_DESC,
            "logic_updated_at": None, "preview": None,
            # 「📋 我的问题」（P2b）：未登录/无用户时给空壳，模板照常渲染
            "has_areas": False, "issue_modules": ISSUE_MODULES,
            "issue_labels": ISSUE_LABELS, "issue_icons": ISSUE_ICONS,
            "issue_max": ISSUE_MAX_ITEMS, "issue_hint": ISSUE_HINT,
            "daily": [], "full": [], "daily_totals": {}, "full_totals": {},
            "handled_rows": [], "handled_total": 0, "recheck_run": None}


def me_context(db, user: dict) -> dict:
    """/me 页面渲染关注领域区 + 口径预览区 + 我的问题区所需上下文（复用已打开的连接）；
    预览/口径/问题取数异常不该让页面挂掉。"""
    ctx = _empty_context()
    if not user or not user.get("id"):
        return ctx
    uid = int(user["id"])
    ctx["area_options"] = area_options(db)
    ctx["labels"] = _label_map(ctx["area_options"])
    ctx["areas"] = selected_areas(db, uid)
    ctx["area_total"] = sum(len(v) for v in ctx["areas"].values())
    ctx["area_logic"] = db.get_area_logic(uid)
    ctx["logic_updated_at"] = db.get_area_logic_updated_at(uid)
    try:
        ctx["preview"] = doc_logic_preview(db, ctx["areas"])
    except Exception:  # noqa: BLE001 - 预览算不出来时页面降级（不影响其它区）
        ctx["preview"] = None
    try:
        ctx.update(issue_context(db, ctx["areas"]))
    except Exception:  # noqa: BLE001 - 问题列表算不出来时页面降级（关注领域区照常）
        pass
    return ctx


def build_me_context(db_path: str, user: dict) -> dict:
    """同 me_context，但自行开关连接（脚本/测试用）。"""
    ctx = _empty_context()
    if not user or not user.get("id"):
        return ctx
    db = IndexDB(db_path)
    try:
        return me_context(db, user)
    finally:
        db.close()


# ── 写操作 ─────────────────────────────────────────────────────────────
def _valid_value(options: dict, dim: str, value: str) -> bool:
    """取值必须落在该维度当前可选集合内（防止手改表单塞入脏值）。"""
    return value in {v for v, _l, _c in (options or {}).get(dim, [])}


def register_me(app, db_path: str = DB_PATH):
    """挂载「关注领域」写路由（/me 的读页面仍在 auth.py）。"""

    def _require_user():
        """返回 (user, redirect_response)；未登录时 user=None 并给出跳转响应。"""
        from auth import auth_enabled, current_user   # 延迟导入：避免与 auth 循环依赖

        user = current_user()
        if user:
            return user, None
        if not auth_enabled():
            return None, redirect("/")
        return None, redirect("/auth/login?next=/me")

    @me_bp.route("/me/areas", methods=["POST"], strict_slashes=False)
    def me_areas():
        user, resp = _require_user()
        if resp is not None:
            return resp

        dim = (request.form.get("dim") or "").strip()
        value = (request.form.get("value") or "").strip()
        action = (request.form.get("action") or "add").strip()

        db = IndexDB(db_path)
        try:
            options = area_options(db)
            if dim not in DIMS:
                flash("⚠️ 未知的关注维度，操作已忽略。", "warn")
            elif not value or len(value) > MAX_VALUE_LEN:
                flash("⚠️ 关注项取值不合法，操作已忽略。", "warn")
            elif action == "remove":
                if db.remove_user_area(int(user["id"]), dim, value):
                    flash(f"✅ 已取消关注：{value}", "ok")
                else:
                    flash(f"ℹ️ 未在关注中：{value}", "info")
            elif not _valid_value(options, dim, value):
                flash("⚠️ 该取值不在可选范围内，操作已忽略。", "warn")
            elif db.add_user_area(int(user["id"]), dim, value):
                flash(f"✅ 已关注：{value}", "ok")
            else:
                flash(f"ℹ️ 已在关注中：{value}", "info")
        finally:
            db.close()
        return redirect("/me")

    @me_bp.route("/me/logic", methods=["POST"], strict_slashes=False)
    def me_logic():
        """口径开关（只存不算）：union=甲·并集 / intersection=乙·交集。"""
        user, resp = _require_user()
        if resp is not None:
            return resp

        logic = (request.form.get("logic") or "").strip()
        if logic not in AREA_LOGICS:
            flash("⚠️ 未知的统计口径，操作已忽略。", "warn")
            return redirect("/me")

        db = IndexDB(db_path)
        try:
            saved = db.set_area_logic(int(user["id"]), logic)
        finally:
            db.close()
        if saved:
            flash(f"✅ 统计口径已保存：{LOGIC_LABELS[logic]}（后续「我的问题列表」按此取数）", "ok")
        else:
            flash("⚠️ 口径保存失败，请稍后重试。", "warn")
        return redirect("/me")

    @me_bp.route("/me/issue", methods=["POST"], strict_slashes=False)
    def me_issue():
        """我的问题：忽略 / 恢复忽略 / 已处理 / 撤销已处理。

        - 忽略：写 ignores（sysmerge 走它自己的独立库，由 ignores 后端注册决定），
          恢复 = 标记 restored_at（不物理删，保留历史）。
        - 已处理：写 handled 表（index.db），恢复同理。
        - 两者都是**全局生效**（谁先点谁生效，不按用户区分）；run / items 数据一律不动，
          所以恢复即时生效、不必重跑检查。
        """
        user, resp = _require_user()
        if resp is not None:
            return resp

        run_id = request.form.get("run_id", type=int)
        item_id = request.form.get("item_id", type=int)
        act = (request.form.get("act") or "").strip()
        who = (user or {}).get("login") or (user or {}).get("name") or ""

        # 「③ 已处理」清单里的撤销：直接按 handled 记录 id 恢复（不需要 run/item）
        if act == "unhandle_id":
            hid = request.form.get("handled_id", type=int)
            db = IndexDB(db_path)
            try:
                ok = bool(hid) and db.restore_handled(hid, restored_by=who)
            finally:
                db.close()
            flash("↩️ 已撤销该「已处理」记录（重新计入「仍存在」）。" if ok
                  else "ℹ️ 该记录已不是生效中的「已处理」。", "ok" if ok else "info")
            return redirect("/me#me-issues")

        if act not in ("ignore", "unignore", "handle", "unhandle") or not run_id or not item_id:
            flash("⚠️ 操作参数不完整，已忽略本次操作。", "warn")
            return redirect("/me#me-issues")

        db = IndexDB(db_path)
        try:
            row = db._conn.execute(
                "SELECT item_type, detail_json FROM items WHERE id=? AND run_id=?",
                (item_id, run_id)).fetchone()
            if not row:
                flash("⚠️ 该条目不存在（可能已被清理），请刷新后重试。", "warn")
                return redirect("/me#me-issues")
            try:
                detail = json.loads(row[1] or "{}")
            except Exception:  # noqa: BLE001
                detail = {}
            run = db.get_run(run_id) or {}
            mk = detail.get("module") or run.get("module_key") or ""
            if mk not in ISSUE_MODULES or not ignores.supports(mk):
                flash("⚠️ 该条目所属模块不支持忽略 / 已处理。", "warn")
                return redirect("/me#me-issues")
            probs = ignores.item_problems(mk, detail, row[0])
            if not probs:
                flash("⚠️ 该条目没有可操作的问题项。", "warn")
                return redirect("/me#me-issues")

            ip = _client_ip()
            n = 0
            if act == "ignore":
                for p in probs:
                    if ignores._backend(db, mk).add_ignore(
                            mk, p["target"], p["kind"], doc_key="",
                            reason="我的问题页", ip=ip):
                        n += 1
                flash(f"✅ 已忽略 {n} 个问题（全局生效，可在本条「已忽略」里恢复）。" if n
                      else "ℹ️ 这些问题的忽略已经生效过了。", "ok" if n else "info")
            elif act == "unignore":
                for p in probs:
                    n += ignores._backend(db, mk).restore_ignores_for(
                        mk, p["target"], p["kind"], p.get("doc_key", ""), ip=ip)
                flash(f"↩️ 已恢复 {n} 个忽略（重新计入「仍存在」）。" if n
                      else "ℹ️ 没有可恢复的忽略。", "ok" if n else "info")
            elif act == "handle":
                for p in probs:
                    if db.mark_handled(mk, p["target"], p["kind"], doc_key="",
                                       note="我的问题页", created_by=who):
                        n += 1
                flash(f"✅ 已标记「已处理」{n} 个问题（全局生效，不再计入「仍存在」）。" if n
                      else "ℹ️ 这些问题的「已处理」已经生效过了。", "ok" if n else "info")
            else:  # unhandle
                for p in probs:
                    n += db.restore_handled_for(mk, p["target"], p["kind"],
                                                p.get("doc_key", ""), restored_by=who)
                flash(f"↩️ 已撤销「已处理」{n} 个问题（重新计入「仍存在」）。" if n
                      else "ℹ️ 没有可撤销的「已处理」。", "ok" if n else "info")
        finally:
            db.close()
        return redirect("/me#me-issues")

    app.register_blueprint(me_bp)
    return me_bp
