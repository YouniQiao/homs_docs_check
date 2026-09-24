"""「我的」→ 关注领域（P2c：两级结构 + 独立配置页 + 细分范围语义）。

口径（用户 2026-09 拍板，替代 P2a 的 4 维平铺 + 并集/交集两种口径）：
  ① 关注的模块（module）—— 本站 4 个：ocr / encheck / linkcheck / sysmerge；不选 = 全部
  ② 关注的文档范围 —— 大类（type = docs.catalog 的 5 个取值）+ 大类内细分：
       指南（harmonyos-guides）      → 细分 Kit（kit@guides）/ IDE 分组（ide@guides）
       API 参考（harmonyos-references）→ 细分 Kit（kit@references）
       FAQ / 版本说明 / 最佳实践      → 无细分
  ③ 语言（lang）—— docs.lang 的取值：cn（中文）/ en（英文）；不选 = 全部语言
  关系：① ⟷ ② ⟷ ③ = 且；② 大类之间 = 或；大类内细分 = 或；大类 + 细分 = 只看该细分
        （勾「指南」再勾「ArkUI」→ 只看 ArkUI 的指南，不是「全部指南 ∪ ArkUI 的参考」）。
        语言是独立的一层「且」（选 cn → 只看中文文档，与文档范围叠加）。

已选值落在 index.db 的 user_areas 表（按用户，UNIQUE(user_id,dim,value)），dim 用
module / type / kit@guides / kit@references / ide@guides / lang；保存 = 整体替换（先清后插，一个事务）。
P2a 的旧 dim（catalog / kit / ide）读时按下表折算，用户在新页面保存一次即迁移：
  catalog → type；kit → kit@guides + kit@references；ide → ide@guides。

文档范围预览：按上面的细分范围语义算命中文档数（scope_where 的 SQL 与 doc_hit 的判定同源），
不再有并集/交集开关（POST /me/logic 保留但已废弃，只存不影响取数）。
module 不是 docs 的列，无法按 doc_key 判定，不参与文档数。

注意：忽略与「已处理」都是**全局**的，不按用户区分（用户 2026-09 拍板）。

「我的问题」（P2b，本文件）——**一条主列表**（2026-09 B 方案：原来的两段合并）：
  单列表（卡片 #me-full，标题「📋 当前待处理（全量）」）= **跨 run 按 item_key 去重后仍存在**
  的条目——优先取「当前全量问题」（module_key=recheck）最新一次成功 run 里该模块的条目
  （recheck 把历史问题跨 run 去重后逐条复核，只写「仍存在」的）；recheck 不覆盖的模块
  （sysmerge）退回该模块最新一次成功 run 的条目（整站全量扫描，天然是「当前全量」）。
  （2026-09 起复核每日 05:30 跑，结果即最新——见 cron；不再与日常 run 合并。）
  **每模块一个页签**；列表里**没有**历史/最新之分——所有还没解决、没忽略的问题都在这一条列表里。
  两段的旧解释已去掉（同事不容易理解两段关系）。
  写操作（忽略/已处理）后回跳保留当前页签（表单带 ft）。
  首次未设关注领域 = 不筛选（全部模块 + 全部文档）并给提示；设了才按领域过滤。
明细列表**与各模块页一致**：列与顺序照搬该模块的 item_columns（如 ocr = 图片 / 语言 /
  识别文字 / 置信度 / 来源文档），图片列渲染缩略图（/media/…，去掉 data/ 前缀）并可点击放大。
忽略 / 已处理都是**展示端过滤**：run/items 数据一律不动（恢复即时生效），
被忽略或被处理完的条目不计入「仍存在」，并单列计数 + 可展开列表（可恢复/撤销）。

路由：
  GET  /me          「我的」（本文件只提供上下文；页面路由在 auth.py）
                    ?ft= 选模块页签（旧 ?dt= 仍认得，等价）；
                    ?fp/?fi/?fh = 列表（仍存在 / 已忽略 / 已处理）各自的页码（见 page_args）
  GET  /me/areas   关注领域配置页（①语言 + ②模块 + ③文档范围；JS 按大类展开细分）
  POST /me/areas   整体保存（module / type / kit@* / ide@* / lang 六类 dim）→ 302 回 /me/areas
  POST /me/logic   已废弃的口径开关（只存不影响取数）→ 302 回 /me
  POST /me/issue   run_id + item_id + act=ignore|unignore|handle|unhandle → 302 回 /me
                   （另支持与模块页对齐的 act=all（全部忽略 / 恢复全部）与
                     act=toggle:<问题序号>（逐条链接忽略 ↔ 恢复，序号 = ignores.item_problems
                     里的全局序号，服务端重算问题列表，不信任前端）；
                     带 X-Requested-With: fetch 时改回 JSON：该条目最新状态 + 逐条问题状态
                     （problems/changed）+ 目标列表，供前端把同一目标的行 / 按钮 / 计数 /
                     逐条链接开关同步更新）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ignores  # noqa: E402
from db import IndexDB  # noqa: E402

DB_PATH = str(BASE_DIR / "index.db")

# ── 两级关注领域（用户 2026-09 拍板重构）────────────────────────────────
# ① 关注的模块（module）：本站 4 个检查模块；不选 = 全部模块
# ② 关注的文档范围：大类（type，取 docs.catalog 的 5 个值）+ 大类内细分
#      type=harmonyos-guides     可细分 kit@guides / ide@guides
#      type=harmonyos-references 可细分 kit@references
#      FAQ / 版本说明 / 最佳实践   无细分
# 关系：① ⟷ ② = 且；② 大类之间 = 或；大类内细分 = 或；大类 + 细分 = 只看该细分
#   （勾「指南」再勾「ArkUI」→ 只看 ArkUI 的指南，而不是「全部指南 ∪ ArkUI 的参考」）。
# 存储：沿用 user_areas 表，dim 用 module / type / kit@guides / kit@references /
# ide@guides / lang。lang 是独立的一层「且」（不选 = 全部语言；选 cn/en = 只要这些语言）。
# P2a 的旧 4 维（catalog / kit / ide）仍可读，按下面这张表折算进新结构
# （用户在新页面保存一次即迁移为上面的 6 个 dim）：
#   catalog → type；kit → kit@guides + kit@references；ide → ide@guides
MODULE_DIM = "module"
TYPE_DIM = "type"
KIT_GUIDES_DIM = "kit@guides"
KIT_REFS_DIM = "kit@references"
IDE_GUIDES_DIM = "ide@guides"
LANG_DIM = "lang"

NEW_DIMS = (MODULE_DIM, TYPE_DIM, KIT_GUIDES_DIM, KIT_REFS_DIM, IDE_GUIDES_DIM, LANG_DIM)
LEGACY_DIMS = ("catalog", "kit", "ide")
ALL_AREA_DIMS = NEW_DIMS + LEGACY_DIMS

# 大类（= docs.catalog 的实际取值）与显示名
TYPE_LABELS = {
    "harmonyos-guides": "指南",
    "harmonyos-references": "API 参考",
    "harmonyos-faqs": "FAQ",
    "harmonyos-releases": "版本说明",
    "best-practices": "最佳实践",
}
TYPE_CATALOGS = tuple(TYPE_LABELS)
TYPE_FULL_LABELS = {k: f"{v}（{k}）" for k, v in TYPE_LABELS.items()}

# 大类 → 细分轴：(dim, 轴标签, docs 列)；只有这两个大类可细分
SUBDIV_DEFS = {
    "harmonyos-guides": ((KIT_GUIDES_DIM, "Kit", "kit"),
                         (IDE_GUIDES_DIM, "IDE 分组", "ide")),
    "harmonyos-references": ((KIT_REFS_DIM, "Kit", "kit"),),
}
SUBDIV_PARENT = {d: t for t, defs in SUBDIV_DEFS.items() for d, _l, _c in defs}
SUBDIV_AXIS_LABEL = {d: lbl for defs in SUBDIV_DEFS.values() for d, lbl, _c in defs}
SUBDIV_HINTS = {
    KIT_GUIDES_DIM: "指南的 Kit 细分（harmonyos-guides 的 Kit 取值，按文档数倒序）",
    IDE_GUIDES_DIM: "指南的 IDE 分组细分（harmonyos-guides 的 IDE 取值）",
    KIT_REFS_DIM: "API 参考的 Kit 细分（harmonyos-references 的 Kit 取值）",
}

# module 建议取值（本站 4 个检查模块）
MODULE_LABELS = {
    "ocr": "图片 OCR 检查（ocr）",
    "encheck": "英文文档检查（encheck）",
    "linkcheck": "链接健康检查（linkcheck）",
    "sysmerge": "系统词合并检查（sysmerge）",
}

# 语言维度（= docs.lang 的实际取值）；不选 = 全部语言
LANG_LABELS = {"cn": "中文（cn）", "en": "英文（en）"}
LANG_ORDER = ("cn", "en")
LANG_HINT = "按文档语言过滤；不选 = 全部语言（中文 + 英文）。"

# 关注领域的关系说明（页面文案统一从这里取，避免多处口径漂移）
SCOPE_RULE = (
    "① 语言 ⟷ ② 模块 ⟷ ③ 文档范围 之间是【且】；"
    "③ 里各大类之间是【或】，勾了某个大类里的细分就只看该细分下的文档"
    "（勾「指南」再勾「ArkUI」= 只看 ArkUI 的指南）。"
    "三者都不选 = 全部。"
)

MAX_VALUE_LEN = 120
_MAX_SAMPLE = 3

me_bp = Blueprint("me_pages", __name__)


# ── 选项/已选（供 /me 页面渲染）────────────────────────────────────────
def _docs_counts(db, col: str, catalog: str = "") -> list[tuple[str, int]]:
    """docs 表某列的非空取值 → [(value, 文档数)]，按文档数倒序（列名白名单，防注入）。

    catalog 非空时只统计该大类下的文档：细分区（Kit / IDE）必须按父大类取，
    否则「API 参考」的 Kit 列表里会混进只在指南里出现的 Kit。
    """
    if col not in ("catalog", "kit", "ide", "lang"):
        return []
    sql = (f"SELECT {col} AS v, COUNT(*) AS c FROM docs"
           f" WHERE {col} IS NOT NULL AND {col}<>''")
    params: list = []
    if catalog:
        sql += " AND catalog=?"
        params.append(catalog)
    sql += f" GROUP BY {col} ORDER BY c DESC, v ASC"
    return [(r[0], int(r[1])) for r in db._conn.execute(sql, params)]


def area_options(db) -> dict:
    """{dim: [(value, label, count)]}（新的 6 个 dim）。

    type（大类）固定 5 项按 docs.catalog 实际取值；细分 dim 从 docs 表 DISTINCT 取、
    按文档数倒序；module 固定 4 项；lang 固定 cn / en（按 LANG_ORDER，附文档数）。
    """
    catalog_counts = dict(_docs_counts(db, "catalog"))
    lang_counts = dict(_docs_counts(db, "lang"))
    out: dict = {
        MODULE_DIM: [(v, MODULE_LABELS[v], 0) for v in MODULE_LABELS],
        TYPE_DIM: [(v, TYPE_LABELS[v], catalog_counts.get(v, 0))
                   for v in TYPE_CATALOGS],
        LANG_DIM: [(v, LANG_LABELS[v], lang_counts.get(v, 0)) for v in LANG_ORDER],
    }
    for parent, defs in SUBDIV_DEFS.items():
        for dim, _axis, col in defs:
            out[dim] = [(v, v, c) for v, c in _docs_counts(db, col, parent)]
    return out


def _label_map(options: dict) -> dict:
    return {dim: {v: lbl for v, lbl, _c in opts} for dim, opts in (options or {}).items()}


def selected_areas(db, user_id: int) -> dict:
    """{dim: [value, ...]} 原样读库（含 P2a 旧 dim，便于页面区分「旧配置」）。"""
    out: dict = {d: [] for d in ALL_AREA_DIMS}
    for r in db.list_user_areas(user_id):
        out.setdefault(r["dim"], []).append(r["value"])
    return out


def areas_effective(areas: dict) -> dict:
    """存储里的关注领域（含 P2a 旧 4 维）→ 归一化到新 6 个 dim 的取值集合。

    旧值折算：catalog → type；kit → kit@guides + kit@references；ide → ide@guides。
    细分值只在其父大类在范围内时保留；若只选了细分、一个大类都没选（旧数据可能出现），
    则把细分所属的大类补进来（否则这些细分会静默失效）。lang 原样保留（cn / en）。
    """
    raw: dict = {d: [] for d in ALL_AREA_DIMS}
    for dim, vals in (areas or {}).items():
        if dim not in raw:
            continue
        for v in vals or []:
            if v and v not in raw[dim]:
                raw[dim].append(v)

    def _merge(*seqs) -> list:
        out: list = []
        for s in seqs:
            for v in s or []:
                if v and v not in out:
                    out.append(v)
        return out

    types = _merge(raw[TYPE_DIM], raw["catalog"])
    subs = {
        KIT_GUIDES_DIM: _merge(raw[KIT_GUIDES_DIM], raw["kit"]),
        IDE_GUIDES_DIM: _merge(raw[IDE_GUIDES_DIM], raw["ide"]),
        KIT_REFS_DIM: _merge(raw[KIT_REFS_DIM], raw["kit"]),
    }
    if not types:  # 只选了细分（旧数据）→ 补上细分所属的大类
        types = [t for t in TYPE_CATALOGS
                 if any(subs[d] for d in subs if SUBDIV_PARENT[d] == t)]
    out: dict = {d: [] for d in NEW_DIMS}
    out[MODULE_DIM] = list(raw[MODULE_DIM])
    out[TYPE_DIM] = [t for t in TYPE_CATALOGS if t in types]
    for dim, vals in subs.items():
        out[dim] = list(vals) if SUBDIV_PARENT[dim] in out[TYPE_DIM] else []
    # 语言：独立的一层「且」，只保留本站认识的语言取值（按 LANG_ORDER 归一）
    out[LANG_DIM] = [v for v in LANG_ORDER if v in raw[LANG_DIM]]
    return out


def areas_scope(areas: dict) -> dict:
    """归一化关注领域 → **细分后**的文档范围。

    {"modules": [...], "langs": [...],
     "groups": [{"catalog","label","narrowed","kits","ides","subs"}...],
     "empty": bool}
    groups 之间是「或」；组内 kits / ides 之间是「或」；有细分时该组 = 大类 ∩ (kits ∪ ides)。
    langs 是独立的一层「且」（不选 = 全部语言）。
    empty=True 表示没选任何文档范围 = 全部文档。
    """
    eff = areas_effective(areas)
    groups: list = []
    for t in eff[TYPE_DIM]:
        kits: list = []
        ides: list = []
        subs: list = []
        for dim, axis, col in SUBDIV_DEFS.get(t, ()):
            vals = eff.get(dim) or []
            if col == "kit":
                kits = vals
            else:
                ides = vals
            for v in vals:
                subs.append({"dim": dim, "axis": axis, "value": v})
        groups.append({"catalog": t, "label": TYPE_LABELS.get(t, t),
                       "narrowed": bool(kits or ides), "kits": kits, "ides": ides,
                       "subs": subs})
    return {"modules": list(eff[MODULE_DIM]), "groups": groups,
            "langs": list(eff[LANG_DIM]),
            "empty": not groups, "effective": eff}


def _norm_langs(vals) -> list:
    """语言取值归一：只认 LANG_ORDER 里的取值（cn / en），去重并保持固定顺序。"""
    got = {str(v) for v in (vals or []) if v}
    return [v for v in LANG_ORDER if v in got]


def _groups_where(groups: list) -> tuple[str, list]:
    """文档范围（大类 + 大类内细分）→ (SQL 片段, 参数)；空串 = 不限文档范围。"""
    clauses: list = []
    params: list = []
    for g in groups or []:
        parts: list = []
        p: list = []
        if g["kits"]:
            parts.append("kit IN (%s)" % ",".join("?" * len(g["kits"])))
            p += list(g["kits"])
        if g["ides"]:
            parts.append("ide IN (%s)" % ",".join("?" * len(g["ides"])))
            p += list(g["ides"])
        if parts:  # 大类 + 细分 = 只看该细分
            clauses.append("(catalog=? AND (%s))" % " OR ".join(parts))
            params.append(g["catalog"])
            params += p
        else:      # 只选大类 = 该类全部文档
            clauses.append("catalog=?")
            params.append(g["catalog"])
    if not clauses:
        return "", []
    return "(" + " OR ".join(clauses) + ")", params


def scope_where(scope: dict) -> tuple[str, list]:
    """细分范围 → (SQL WHERE 片段, 参数)；片段为空串 = 不限文档（全部文档）。

    语言（langs）是独立的一层「且」：与文档范围的 OR 组用 AND 连接
    （选 cn + 指南 → 中文的指南）。都不选 = 全部文档。
    """
    scope = scope or {}
    clauses: list = []
    params: list = []
    langs = _norm_langs(scope.get("langs"))
    if langs:
        clauses.append("lang IN (%s)" % ",".join("?" * len(langs)))
        params += langs
    gw, gp = _groups_where(scope.get("groups") or [])
    if gw:
        clauses.append(gw)
        params += gp
    if not clauses:
        return "", []
    return "(" + " AND ".join(clauses) + ")", params


def doc_hit(scope: dict, meta: dict, detail: dict | None = None) -> bool:
    """某文档 / 问题条目是否落在细分范围内（与 scope_where 同一套判定，两处必须一致）。

    meta 优先（docs 表的 catalog/kit/ide/lang），取不到时回落条目 detail。
    lang 是独立的一层「且」，先判（语言不在范围内 → 直接不算命中）。
    """
    scope = scope or {}
    meta = meta or {}
    detail = detail or {}
    langs = _norm_langs(scope.get("langs"))
    if langs:
        lg = str(meta.get("lang") or detail.get("lang") or "")
        if lg not in langs:
            return False
    groups = scope.get("groups") or []
    if not groups:
        return True
    cat = meta.get("catalog") or detail.get("catalog") or ""
    for g in groups:
        if cat != g["catalog"]:
            continue
        if not g["narrowed"]:
            return True
        kit = meta.get("kit") or ""
        ide = meta.get("ide") or ""
        return bool((g["kits"] and kit in g["kits"]) or (g["ides"] and ide in g["ides"]))
    return False


# ── 文档范围预览：按细分范围语义算命中文档（只读 docs 表，不写数据）──────────────
def scope_preview(db, scope: dict, sample_n: int = _MAX_SAMPLE) -> dict:
    """文档范围口径的预览数据：命中文档数 + 每个大类明细 + 2-3 个示例文档。

    与 scope_where 用同一段 SQL 条件，保证「预览数字」与「我的问题」实际过滤一致。
    module 不是 docs 的列，无法按 doc_key 判定，不参与文档数。
    """
    total = int(db._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
    langs = _norm_langs((scope or {}).get("langs"))
    where, params = scope_where(scope)
    hit = int(db._conn.execute(
        "SELECT COUNT(*) FROM docs" + (f" WHERE {where}" if where else ""),
        params).fetchone()[0])
    rows = db._conn.execute(
        "SELECT doc_key, title, catalog, kit, ide, lang FROM docs"
        + (f" WHERE {where}" if where else "")
        + " ORDER BY catalog, kit, title LIMIT ?", params + [sample_n]).fetchall()

    groups: list = []
    for g in (scope or {}).get("groups") or []:
        # 每个大类的明细也要吃语言条件（否则明细行加起来 ≠ 命中总数）
        gw, gp = scope_where({"groups": [g], "langs": langs})
        n = int(db._conn.execute(
            f"SELECT COUNT(*) FROM docs WHERE {gw}", gp).fetchone()[0])
        subs: list = []
        for dim, axis, col in SUBDIV_DEFS.get(g["catalog"], ()):
            vals = g["kits"] if col == "kit" else g["ides"]
            for v in vals:
                c = int(db._conn.execute(
                    f"SELECT COUNT(*) FROM docs WHERE catalog=? AND {col}=?"
                    + (" AND lang IN (%s)" % ",".join("?" * len(langs)) if langs else ""),
                    (g["catalog"], v, *langs)).fetchone()[0])
                subs.append({"axis": axis, "value": v, "count": c,
                             "count_fmt": f"{c:,}"})
        groups.append({
            "catalog": g["catalog"], "label": g["label"],
            "narrowed": g["narrowed"], "subs": subs,
            "kits": g["kits"], "ides": g["ides"],
            "count": n, "count_fmt": f"{n:,}",
        })

    return {
        "has_scope": bool(groups) or bool(langs),
        "modules": list((scope or {}).get("modules") or []),
        "langs": langs,
        "lang_labels": [LANG_LABELS.get(v, v) for v in langs],
        "total_docs": total, "total_docs_fmt": f"{total:,}",
        "hit": {"count": hit, "count_fmt": f"{hit:,}",
                "samples": [{"doc_key": r[0], "title": r[1] or r[0], "catalog": r[2],
                             "kit": r[3] or "", "ide": r[4] or "", "lang": r[5] or ""}
                            for r in rows]},
        "groups": groups,
        "all_docs": not groups and not langs,
    }



# ── 我的问题（P2b）──────────────────────────────────────────────────────
# 4 个检查模块（module 维度只作用于问题所属模块；kit/ide 只对 guides/references 有意义）
ISSUE_MODULES = ("ocr", "encheck", "linkcheck", "sysmerge")
ISSUE_LABELS = {"ocr": "图片 OCR 检查", "encheck": "英文文档检查",
                "linkcheck": "链接健康检查", "sysmerge": "系统合并整改"}
ISSUE_ICONS = {"ocr": "🔍", "encheck": "🌐", "linkcheck": "🔗", "sysmerge": "🧩"}
# 「当前全量问题」（recheck）覆盖的模块；其余（sysmerge）取自身最新一次成功 run
RECHECK_MODULES = ("ocr", "encheck", "linkcheck")
# ── 分页（用户 2026-09 要求，复刻模块页 task_detail 的翻页体验）──────────────
# 单列表（原两段合并后）只需**一套**页码参数：fp（仍存在）/ fi（已忽略）/ fh（已处理）；
#   每页 ISSUE_PAGE_SIZE 条，组内三个列表各自独立分页（各自的总数 / 总页数）；
#   分页只对**当前页签**那个模块生效（用户一次只看一个模块），顶部数字 / 页签数字
#   仍是该模块的真实总数（不受分页影响）。
# 旧的两段参数（dp/di/dh，每日增量段）不再使用，但**不报错**：page_args 会把它当
#   fp/fi/fh 的别名读一次（旧书签 / 旧链接仍然落在同一页），未知参数一律忽略。
ISSUE_PAGE_SIZE = 30
ISSUE_PAGE_PARAMS = {                 # 列表 → 页码 URL 参数名（单列表，只有一套）
    "open": "fp", "ignored": "fi", "handled": "fh",
}
ISSUE_PAGE_ARGS = ("fp", "fi", "fh")            # 顺序 = 表单隐藏字段 / 回跳顺序
# 旧参数 → 新参数（只在新的缺省时才读；保留旧链接可用，不破坏已有 URL）
ISSUE_PAGE_ALIASES = {"fp": ("dp",), "fi": ("di",), "fh": ("dh",)}
ISSUE_PAGE_SPAN = 5                   # 页码条里当前页左右各显示几个页码
ISSUE_MAX_ITEMS = ISSUE_PAGE_SIZE     # 兼容旧名（语义已变为「每页条数」）
ISSUE_HINT = ("""「仍存在」= 最新一次检查里还有、且没被忽略/处理掉的条目；"""
              """忽略 / 已处理都是全局的（谁先点谁生效），被处理完的不计入「仍存在」但保留可见，可随时恢复。""")

# ── 明细列：照搬各模块页的 item_columns（/me 列表与模块页同一套列与顺序）──────
# 例外（用户 2026-09 要求）：/me 列表里**不显示**这些列（模块页仍照常显示）。
#   ocr 的「置信度」在「我的问题」里没用（要判断的是「图里有没有中文」），
#   列少了宽度还给「识别文字」和「来源文档」，故按模块列名过滤掉。
HIDDEN_COLUMNS = {"ocr": ("confidence",)}

_COLUMNS_CACHE: dict | None = None


def item_columns(mk: str) -> list[tuple]:
    """模块 key → [(字段, 标签, 渲染类型)]。

    与模块页 framework._norm_columns 同一套规则：模块可定义二元组 (f, label)
    或三元组 (f, label, render)；渲染类型缺省 text。取不到配置时返回空（页面降级）。
    再按 HIDDEN_COLUMNS 去掉该模块在 /me 列表里不展示的列。
    """
    global _COLUMNS_CACHE
    if _COLUMNS_CACHE is None:
        _COLUMNS_CACHE = {}
        try:
            web_dir = str(Path(__file__).resolve().parent)
            if web_dir not in sys.path:
                sys.path.insert(0, web_dir)
            from modules import MODULES
            for m in MODULES:
                hide = tuple(HIDDEN_COLUMNS.get(m["key"], ()))
                cols: list = []
                for col in m.get("item_columns") or []:
                    if col[0] in hide:      # /me 不展示的列（模块页仍有）
                        continue
                    cols.append((col[0], col[1], col[2] if len(col) > 2 else "text"))
                _COLUMNS_CACHE[m["key"]] = cols
        except Exception:  # noqa: BLE001 - 取不到列配置也不该让页面挂掉
            _COLUMNS_CACHE = {}
    return list(_COLUMNS_CACHE.get(mk) or [])


def _link_rows(mk: str, detail: dict, probs: list) -> dict:
    """link_list 列的渲染数据：{字段: [{idx, text, url, target, status, ignored, handled}]}。

    与模块页一样逐条链接标忽略态；``idx`` = 该问题在 `ignores.item_problems` 里的
    **全局序号**（与模块页 `.ign-mini` 的 data-idx 同一口径），前端按它提交
    ``act=toggle:<idx>``，服务端据此重算问题列表；``target`` 供前端跨两段匹配同一条链接。
    """
    by_kind: dict = {}
    for idx, p in enumerate(probs or []):
        if p.get("inline"):
            by_kind.setdefault(p["kind"], []).append((idx, p))
    if not by_kind:
        return {}
    kind2field = {k: f for k, _l, _c, f in ignores.kinds_of(mk) if f}
    out: dict = {}
    for kind, ps in by_kind.items():
        f = kind2field.get(kind)
        if not f:
            continue
        raw = detail.get(f) or []
        rows: list = []
        for i, (idx, p) in enumerate(ps):
            entry = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
            rows.append({"idx": idx,
                         "text": entry.get("text") or p.get("label") or "",
                         "url": p.get("target") or entry.get("url") or "",
                         "target": p.get("target") or "",
                         "status": entry.get("status") or "",
                         "ignored": bool(p.get("ignored")),
                         "handled": bool(p.get("handled"))})
        if rows:
            out[f] = rows
    return out



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
    """某次 run 的条目；同一 item_key 只保留一条（**优先带问题的那条**，见下）。

    每条带 ``run_id``（= 这条 item 真正所属的 run）——「全量」列表会合并**多个 run**
    的条目，忽略/已处理表单里的 ``run_id`` + ``item_id`` 必须成对，否则
    ``POST /me/issue`` 的 `items WHERE id=? AND run_id=?` 查不到（操作报「条目不存在」）。
    """
    out: dict = {}
    for iid, key, itype, dj in db._conn.execute(
            "SELECT id, item_key, item_type, detail_json FROM items"
            " WHERE run_id=? ORDER BY id", (run_id,)):
        try:
            detail = json.loads(dj or "{}")
        except Exception:  # noqa: BLE001
            detail = {}
        rec = {"id": iid, "run_id": run_id, "item_key": key,
               "item_type": itype, "detail": detail}
        # 去重键 = **模块 + item_key**：复核一个 run 混装三模块，同一 doc 在 encheck 与
        # linkcheck 各有一条**同名 item_key**（都用 doc_key），只按 item_key 去重会互相覆盖，
        # 导致某个模块的条目整批丢失（/me 比 /recheck 少）。
        mkk = detail.get("module") or ""
        k = f"{mkk}|{key}" if key else f"{mkk}|#{iid}"
        prev = out.get(k)
        # 同键多条时优先保留带问题的那条（复核对同一文档既写带问题的条目、又写空壳 problem）。
        if prev is None or (_item_has_probs(rec) and not _item_has_probs(prev)):
            out[k] = rec
    return list(out.values())


def _item_has_probs(rec: dict) -> bool:
    """这条 item 是否解析出问题（同 item_key 多条时的取舍依据）。"""
    d = rec.get("detail") or {}
    mk = d.get("module") or ""
    if not mk:
        return False
    try:
        return bool(ignores.item_problems(mk, d, rec.get("item_type") or ""))
    except Exception:  # noqa: BLE001 - 解析失败按「无问题」保守处理
        return False


def _docs_meta(db, keys) -> dict:
    """doc_key → {title, catalog, kit, ide, lang, url}（分批 IN，避免超长 SQL）。"""
    keys = sorted({k for k in keys if k})
    out: dict = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        sql = ("SELECT doc_key, title, catalog, kit, ide, url, lang FROM docs"
               " WHERE doc_key IN (%s)" % ",".join("?" * len(chunk)))
        for dk, title, cat, kit, ide, url, lang in db._conn.execute(sql, chunk):
            out[dk] = {"title": title or "", "catalog": cat or "", "kit": kit or "",
                       "ide": ide or "", "url": url or "", "lang": lang or ""}
    return out


def _area_selection(areas: dict) -> tuple[dict, list]:
    """已选关注领域 → (细分范围 scope, 已选模块列表)。

    范围判定统一走 areas_scope / scope_where（细分范围语义），问题列表的条目过滤用
    同一套判定（doc_hit），保证「文档范围预览的数字」与「实际展示的条目」一致。
    """
    scope = areas_scope(areas)
    return scope, list(scope.get("modules") or [])


def _doc_hit(meta: dict, detail: dict, scope: dict) -> bool:
    """文档范围口径：条目所属文档是否在范围内（未选任何文档范围 = 不限制）。"""
    return doc_hit(scope, meta, detail)


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
    # 复核 run 写入的条目（item_type=still/problem）一律算问题——它们是复核判定「仍存在」
    # 的权威结论；个别新 schema 未解析出 probs 也不能当非问题丢掉（否则 /me 比 /recheck 少）。
    if item_type in ("still", "problem"):
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
                       scope: dict, rules: dict,
                       handled_rules: dict, source: str = "", link: str = "") -> dict:
    """把一次 run 的条目整理成一个模块分组（含三态计数 + 三条列表）。

    条目是否算「我的」走细分范围语义（scope）：未选任何文档范围 = 不限（全部文档）；
    命中大类但不在该大类的细分里 = 领域外（计入 n_out）。
    """
    g = {"module": mk, "label": ISSUE_LABELS.get(mk, mk), "icon": ISSUE_ICONS.get(mk, ""),
         "source": source, "run_id": run["id"] if run else None,
         "detail_link": link,
         "run_at": _fmt_time((run or {}).get("started_at")),
         "columns": item_columns(mk),      # 列 = 该模块页的 item_columns（顺序一致）
         "checked": len(raw_items), "n_total": 0, "n_open": 0, "n_ignored": 0,
         "n_handled": 0, "n_out": 0, "n_normal": 0,
         "open": [], "ignored": [], "handled": []}
    for it in raw_items:
        d = it["detail"] or {}
        dk = d.get("doc_key") or it["item_key"] or ""
        meta = metas.get(dk) or {}
        if not _doc_hit(meta, d, scope):
            g["n_out"] += 1
            continue
        probs, st = _problem_state(mk, d, it["item_type"], rules, handled_rules)
        if not _is_problem_item(mk, it["item_type"], d, probs):
            g["n_normal"] += 1
            continue
        summary, extra = _item_summary(mk, d, it["item_type"], probs)
        bucket = st["status"] if st["status"] in ("open", "ignored", "handled") else "open"
        # 显示用标题：兜底到 doc_key / item_key 时另存 doc_title=""，模板据此改渲染
        # 「查看文档 ↗」（不再吐 raw doc_key）。
        title = (meta.get("title") or d.get("doc_title") or d.get("title")
                 or dk or it["item_key"] or "")
        row = {
            # run_id = **这条 item 真正所属的 run**（全量组会合并多个 run 的条目；
            # 忽略/已处理表单提交 run_id + item_id，必须成对）
            "id": it["id"], "run_id": it.get("run_id") or g["run_id"], "module": mk,
            "module_label": g["label"],
            "title": title, "doc_title": "" if title == dk else title,
            "doc_key": dk, "catalog": meta.get("catalog") or d.get("catalog") or "",
            "kit": meta.get("kit") or d.get("kit") or "", "ide": meta.get("ide") or "",
            "lang": meta.get("lang") or d.get("lang") or "", "item_type": it["item_type"],
            "summary": summary, "extra": extra, "status": st["status"],
            "state": bucket,
            "n_probs": st["n_probs"], "n_open": st["n_open"],
            "all_ignored": st["all_ignored"], "all_handled": st["all_handled"],
            "url": d.get("url") or d.get("doc_url") or meta.get("url") or "",
            "can_act": bool(probs),
            # 该条目上每个问题的「目标」：前端按（模块 + 目标）匹配两段里的同一目标并同步
            "targets": [p["target"] for p in probs],
            # 明细渲染：整列照搬模块页（detail = items.detail_json 原样；links = link_list 列数据）
            "detail": d, "links": _link_rows(mk, d, probs),
            "n_ign": st["n_ignored"], "n_hand": st["n_handled"],
        }
        g["n_total"] += 1
        g["n_" + bucket] += 1
        g[bucket].append(row)
    # 不在这里截断：三个列表都完整构建（总数 / 页签数字从这里取），
    # 「按页切片」只对当前页签的分组做（见 _apply_pagination）。
    return g


def _totals(groups: list) -> dict:
    keys = ("n_total", "n_open", "n_ignored", "n_handled", "n_out", "n_normal")
    return {k: sum(g[k] for g in groups) for k in keys}


# ── 分页：切片 + 分页控件数据（复刻模块页 task_detail 的 .pagination）────────
def page_args(args) -> dict:
    """读页码 URL 参数 → {参数名: 页码}（缺省 / 非正整数 / 脏值一律按 1）。

    只认一套参数：fp / fi / fh（仍存在 / 已忽略 / 已处理）。旧参数 dp / di / dh
    （原「每日增量」段）当别名读一次 —— 只有对应的新参数缺省时才生效 —— 于是旧书签
    不报错、仍落在同一页；其它未知参数一律忽略（不报错）。
    ``args`` 是 Flask 的 request.args（任何带 ``get`` 的映射都行），便于脚本 / 测试
    直接喂字典。越界（大于总页数）由 ``_pager_view`` 按真实总数钳制。
    """
    out: dict = {}
    for name in ISSUE_PAGE_ARGS:
        raw = None
        try:
            raw = args.get(name) if hasattr(args, "get") else None
            if raw in (None, ""):
                for alt in ISSUE_PAGE_ALIASES.get(name, ()):
                    raw = args.get(alt) if hasattr(args, "get") else None
                    if raw not in (None, ""):
                        break
            out[name] = max(1, int(str(raw if raw not in (None, "") else "1")))
        except (TypeError, ValueError):
            out[name] = 1
    return out


def _page_numbers(page: int, total_pages: int, span: int = ISSUE_PAGE_SPAN) -> list:
    """页码条：[1, 2, None, 5, 6, 7, 8, 9, None, 19, 20]（None = 省略号）。

    首尾页恒显，中间围绕当前页开窗（页数少时直接全列）。
    """
    if total_pages <= span + 4:
        return list(range(1, total_pages + 1))
    mid_start = max(3, page - span // 2)
    mid_end = min(total_pages - 2, mid_start + span - 1)
    mid_start = max(3, mid_end - span + 1)
    out: list = [1, 2]
    if mid_start > 3:
        out.append(None)
    out += list(range(mid_start, mid_end + 1))
    if mid_end < total_pages - 2:
        out.append(None)
    out += [total_pages - 1, total_pages]
    return out


def _me_url(qs: dict, anchor: str = "#me-issues") -> str:
    """回 /me 的地址：保留非空参数（dt / ft / 各页码），带锚点。"""
    from urllib.parse import urlencode

    q = {k: v for k, v in (qs or {}).items() if v not in (None, "")}
    return "/me" + (("?" + urlencode(q)) if q else "") + (anchor or "")


def _clamp_page(total: int, page: int) -> int:
    """页码按真实总数钳制到 [1, 总页数]（越界 = 最后一页；0 / 脏值 = 第 1 页）。"""
    total_pages = max(1, (int(total or 0) + ISSUE_PAGE_SIZE - 1) // ISSUE_PAGE_SIZE)
    return max(1, min(int(page or 1), total_pages))


def _pager_view(rows: list, page: int, qs: dict, name: str,
                anchor: str = "#me-issues") -> tuple[list, dict]:
    """一个列表 → (当页行, 分页控件数据)。

    页码越界（0 / 负数 / 超过总页数）按真实总数钳制到 [1, total_pages]；
    链接保留 ``qs`` 里的全部参数（dt / ft / 另一段的页码 / 本列表其它页码无关），
    只把本列表的页码参数换成目标页（本列表参数名 = ``name``）。
    """
    rows = list(rows or [])
    total = len(rows)
    total_pages = max(1, (total + ISSUE_PAGE_SIZE - 1) // ISSUE_PAGE_SIZE)
    page = _clamp_page(total, page)
    start = (page - 1) * ISSUE_PAGE_SIZE
    keep = {k: v for k, v in (qs or {}).items() if k != name}

    def _link(n: int) -> str:
        return _me_url({**keep, name: n}, anchor)

    pager = {"name": name, "page": page, "total": total, "total_pages": total_pages,
             "size": ISSUE_PAGE_SIZE,
             "prev": _link(page - 1) if page > 1 else "",
             "next": _link(page + 1) if page < total_pages else "",
             "items": [{"n": n, "cur": n == page, "href": _link(n)} if n else None
                       for n in _page_numbers(page, total_pages)]}
    return rows[start:start + ISSUE_PAGE_SIZE], pager


def _apply_pagination(g: dict | None, pages: dict, qs: dict, anchor: str) -> None:
    """只对**当前页签**的分组按页切片（仍存在 / 已忽略 / 已处理 各自独立分页）。

    顶部数字、页签数字用的是分组里的真实总数（n_total / n_open / n_ignored / n_handled），
    不受分页影响；这里只把三个列表换成本页切片 + 挂上 ``pager_<列表>`` 控件数据。
    """
    if not g:
        return
    for bucket, name in ISSUE_PAGE_PARAMS.items():
        rows, pager = _pager_view(g.get(bucket) or [], (pages or {}).get(name, 1),
                                  qs, name, anchor)
        g[bucket] = rows
        g["pager_" + bucket] = pager


def _tab_href(mk: str) -> str:
    """页签链接：切到该模块（页码一律回到第 1 页，不必把 fp=1 写进 URL）。"""
    return _me_url({"ft": mk}, "#me-full")


def _tab_items(groups: list, hrefs: dict | None = None) -> list[dict]:
    """两段各自的「模块页签」：[{key,label,icon,n_open,n_ignored,n_handled,n_total}]。

    页签名 / 顺序 = 该段分组顺序（= 关注模块顺序，未选模块时 = ISSUE_MODULES 顺序），
    与各模块页的页签同款；数字取「仍存在」（与列表里的行数一致）。
    """
    return [{"key": g["module"], "label": g["label"], "icon": g["icon"],
             "n_open": g["n_open"], "n_ignored": g["n_ignored"],
             "n_handled": g["n_handled"], "n_total": g["n_total"],
             "run_id": g["run_id"],
             # 页签链接由 _tab_href 算好（带 dt/ft + 页码参数），模板直接用
             "href": (hrefs or {}).get(g["module"]) or "/me#me-issues"}
            for g in groups]


def _pick_group(groups: list, key: str) -> dict | None:
    """按页签 key 选分组；key 为空 / 非法时回落第一个（页签永远有内容可看）。"""
    if not groups:
        return None
    for g in groups:
        if g["module"] == key:
            return g
    return groups[0]


def _is_fetch() -> bool:
    """请求是否来自站内 fetch（模板劫持表单提交时带 X-Requested-With: fetch）。

    是 → 回 JSON（前端原地同步，不必整页刷新）；否 → 照旧 302 回 /me（无 JS 也能用）。
    """
    try:
        return request.headers.get("X-Requested-With", "").lower() == "fetch"
    except Exception:  # noqa: BLE001 - 无请求上下文时按非 fetch 处理
        return False


def _json_state(payload: dict):
    """fetch 请求的 JSON 响应（延迟导入 jsonify，避免模块级依赖）。"""
    from flask import jsonify

    return jsonify(payload)


def _back_to_me() -> str:
    """写操作后回 /me 的地址：保留当前模块页签与页码（表单里带 ft + fp/fi/fh）。

    不带这些字段时退回「第一个模块的页签 / 第 1 页」，否则用户在「链接健康」第 3 页
    点忽略后会被弹回「图片 OCR」第 1 页（看起来像操作没生效）。
    旧表单里的 dt / dp / di / dh 仍认（当别名），旧页面缓存提交也不会丢状态。
    """
    qs: list = []
    ft = (request.form.get("ft") or request.form.get("dt") or "").strip()
    if ft in ISSUE_MODULES:
        qs.append(f"ft={ft}")
    for name in ISSUE_PAGE_ARGS:      # 页码：只带 >1 的（第 1 页不必写在 URL 里）
        raw = request.form.get(name)
        if raw in (None, ""):
            for alt in ISSUE_PAGE_ALIASES.get(name, ()):
                raw = request.form.get(alt)
                if raw not in (None, ""):
                    break
        try:
            p = int(str(raw or "1"))
        except ValueError:
            p = 1
        if p > 1:
            qs.append(f"{name}={p}")
    return "/me" + (("?" + "&".join(qs)) if qs else "") + "#me-full"


def issue_context(db, areas: dict, tab: str = "", pages: dict | None = None) -> dict:
    """「📋 我的问题」单列表（当前待处理 = 全量）+ 已处理汇总。

    单列表带一列模块页签（``tab`` 是当前选中的模块 key）：返回的 ``full`` 是**全部模块**的
    分组（页签数字从这里取），``full_active`` 才是当前页签要渲染的那一组（模板只渲染它，
    避免一次铺 4 个模块的明细表）。

    ``pages``：页码参数 fp / fi / fh（见 page_args）——只对当前页签的分组按页切片；
    缺省全为第 1 页（脚本 / 测试可直接喂字典）。
    """
    scope, mods = _area_selection(areas)
    out = {
        "has_areas": bool((scope or {}).get("groups") or mods
                          or (scope or {}).get("langs")),
        "scope": scope,
        "issue_modules": ISSUE_MODULES, "issue_labels": ISSUE_LABELS,
        "issue_icons": ISSUE_ICONS, "issue_page_size": ISSUE_PAGE_SIZE,
        "issue_hint": ISSUE_HINT,
        "full": [], "full_totals": {},
        "handled_rows": [], "handled_total": 0, "recheck_run": None,
    }
    try:
        rules = ignores.active_map(db)
        handled_rules = db.active_handled_map()
    except Exception:  # noqa: BLE001 - 取不到忽略/已处理时按「没有」处理（页面照常出）
        rules, handled_rules = {}, {}

    # 单列表（全量）：recheck 最新一次成功 run（每日 05:30）+ sysmerge 自身最新 run
    rc = _latest_success_run(db, "recheck")
    out["recheck_run"] = {"id": rc["id"], "run_at": _fmt_time(rc["started_at"])} if rc else None
    # 复核一次跑三模块、条目混在同一个 run 里 → 先按 detail.module 分桶，各组只取本模块的。
    rc_by_mod: dict = {}
    if rc:
        for it in _run_items(db, rc["id"]):
            rmk = (it["detail"] or {}).get("module") or ""
            if rmk:
                rc_by_mod.setdefault(rmk, []).append(it)
    full_runs: dict = {}
    for mk in ISSUE_MODULES:
        if mods and mk not in mods:
            continue
        if mk in RECHECK_MODULES:
            # 全量 = 复核 run 里**本模块**的条目（复核 2026-09 起每日 05:30 跑，结果即最新；
            # 不再与日常 run 合并——见 cron）。
            items = rc_by_mod.get(mk, []) if rc else []
            src = (f"🗓️ 当前全量问题 #{rc['id']}" if rc else "")
            full_runs[mk] = (rc, items, src)
        else:
            run = _latest_success_run(db, mk)
            full_runs[mk] = (run, _run_items(db, run["id"]) if run else [],
                             f"🧩 {ISSUE_LABELS[mk]} #{run['id']}（整站全量扫描）" if run else "")

    # 文档元信息一次批量取（单列表共用）
    keys: list = []
    for run, items, _s in full_runs.values():
        keys += [(it["detail"] or {}).get("doc_key") or it["item_key"] for it in items]
    metas = _docs_meta(db, keys)

    def _full_link(mk, run):
        if not run:
            return ""
        return (f"/recheck/run/{run['id']}?tab={mk}" if mk in RECHECK_MODULES
                else f"/{mk}/run/{run['id']}")

    out["full"] = [_build_issue_group(db, mk, run, items, metas, scope, rules, handled_rules,
                                      source=src, link=_full_link(mk, run))
                   for mk, (run, items, src) in full_runs.items()]
    out["full_totals"] = _totals(out["full"])

    # 模块页签 + 当前选中的分组（tab 来自 ?ft=；旧 ?dt= 由路由折算）
    out["full_active"] = _pick_group(out["full"], (tab or "").strip())
    out["full_tab"] = out["full_active"]["module"] if out["full_active"] else ""

    # 分页（用户 2026-09 要求）：单列表只需**一套**页码参数 fp / fi / fh。
    #   只对**当前页签**的分组按页切片（用户一次只看一个模块）；顶部数字、页签数字
    #   仍是该模块的真实总数（n_total / n_open / n_ignored / n_handled，不受分页影响）。
    pages = {k: (pages or {}).get(k, 1) for k in ISSUE_PAGE_ARGS}
    # 先按真实总数把页码钳制到有效范围（越界 → 最后一页），再用钳制后的页码算所有链接
    # （页签链接 / 分页链接 / 表单隐藏字段都只带有效页码，URL 不会残留 fp=9 这种脏值）。
    act_g = out["full_active"]
    for bucket, name in ISSUE_PAGE_PARAMS.items():
        pages[name] = _clamp_page(len((act_g or {}).get(bucket) or []), pages[name])
    out.update(pages)                     # 模板里表单的隐藏字段（回跳时保留页码）
    out["issue_pages"] = pages
    out["issue_page_args"] = ISSUE_PAGE_ARGS
    out["issue_page_size"] = ISSUE_PAGE_SIZE
    qs = {"ft": out["full_tab"], **pages}
    _apply_pagination(out["full_active"], pages, qs, "#me-full")
    # 页签链接：切模块时页码回到第 1 页（第 1 页不写进 URL）
    out["full_tabs"] = _tab_items(
        out["full"], {g["module"]: _tab_href(g["module"]) for g in out["full"]})

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


def scope_summary(scope: dict) -> list[dict]:
    """细分范围的文字回显（/me 只读区与 /me/areas 共用）。

    每条：``{label, catalog, detail, detail_items, narrowed, count}``
    （``detail`` 是「、」拼接的字符串；``detail_items`` 是细分取值列表，
    供 /me 的胶囊分组渲染 —— 每个细分一个独立小胶囊）。
    """
    out: list = []
    for g in (scope or {}).get("groups") or []:
        items = [s["value"] for s in g["subs"]]
        detail = ("、".join(items) if g["narrowed"] else "全部")
        out.append({"label": g["label"], "catalog": g.get("catalog", ""),
                    "detail": detail, "detail_items": items,
                    "narrowed": g["narrowed"], "count": len(g["subs"])})
    return out


def lang_summary(scope: dict) -> list[dict]:
    """语言维度的只读回显：[{value, label}]（不选 = 全部语言 → 空列表）。"""
    return [{"value": v, "label": LANG_LABELS.get(v, v)}
            for v in _norm_langs((scope or {}).get("langs"))]


def build_areas_view(db, eff: dict, options: dict) -> dict:
    """GET /me/areas 渲染用：①语言 + ②模块 + ③大类（含细分选项与已选回填）。"""
    checked = {d: set(eff.get(d) or []) for d in NEW_DIMS}
    opt_map = options or {}
    modules = [{"value": v, "label": MODULE_LABELS[v],
                "checked": v in checked[MODULE_DIM]} for v in MODULE_LABELS]
    lang_counts = dict(_docs_counts(db, "lang"))
    langs = [{"value": v, "label": LANG_LABELS[v],
              "count_fmt": f"{lang_counts.get(v, 0):,}" if lang_counts.get(v) else "",
              "checked": v in checked[LANG_DIM]} for v in LANG_ORDER]
    type_counts = dict(_docs_counts(db, "catalog"))
    types: list = []
    for t in TYPE_CATALOGS:
        subs: list = []
        for dim, axis, col in SUBDIV_DEFS.get(t, ()):
            sel = checked[dim]
            opts: list = []
            seen: set = set()
            for v, lbl, c in opt_map.get(dim, []):
                seen.add(v)
                opts.append({"value": v, "label": lbl,
                             "count_fmt": f"{c:,}" if c else "", "checked": v in sel})
            for v in sorted(sel - seen):   # 已不在选项里的历史值也回填（可取消）
                opts.append({"value": v, "label": v, "count_fmt": "", "checked": True})
            subs.append({"dim": dim, "field": _form_name(dim), "axis": axis,
                         "hint": SUBDIV_HINTS.get(dim, ""),
                         "n_selected": len(sel), "options": opts})
        types.append({"value": t, "label": TYPE_LABELS[t],
                      "full_label": TYPE_FULL_LABELS[t],
                      "count_fmt": f"{type_counts.get(t, 0):,}",
                      "checked": t in checked[TYPE_DIM], "subs": subs})
    return {"modules": modules, "types": types, "langs": langs,
            "lang_dim": LANG_DIM, "lang_hint": LANG_HINT}


def areas_page_context(db, user: dict) -> dict:
    """GET /me/areas 上下文：选项 + 已选回填 + 当前已关注领域文档范围预览（含语言）。"""
    uid = int(user["id"])
    options = area_options(db)
    raw = selected_areas(db, uid)
    eff = areas_effective(raw)
    scope = areas_scope(raw)
    ctx = {
        "area_options": options, "raw_areas": raw, "areas_eff": eff,
        "areas_view": build_areas_view(db, eff, options),
        "area_total": sum(len(v) for v in eff.values()),
        "has_legacy": any(raw.get(d) for d in LEGACY_DIMS),
        "module_labels": MODULE_LABELS, "module_dim": MODULE_DIM,
        "lang_labels": LANG_LABELS, "lang_dim": LANG_DIM,
        "lang_hint": LANG_HINT,
        "type_labels": TYPE_LABELS, "type_full_labels": TYPE_FULL_LABELS,
        "subdiv_parent": SUBDIV_PARENT, "subdiv_axis": SUBDIV_AXIS_LABEL,
        "subdiv_hints": SUBDIV_HINTS, "subdiv_defs": SUBDIV_DEFS,
        "scope_rule": SCOPE_RULE, "scope": scope,
        "scope_summary": scope_summary(scope),
        "lang_summary": lang_summary(scope),
    }
    try:
        ctx["preview"] = scope_preview(db, scope)
    except Exception:  # noqa: BLE001 - 预览算不出来不影响保存/回填
        ctx["preview"] = None
    return ctx


def _empty_context() -> dict:
    ctx = {"areas": {d: [] for d in ALL_AREA_DIMS},
            "areas_eff": {d: [] for d in NEW_DIMS},
            "area_options": {}, "labels": {}, "areas_view": {"modules": [], "types": [], "langs": []},
            "area_total": 0, "has_legacy": False,
            "module_labels": MODULE_LABELS, "module_dim": MODULE_DIM,
            "lang_labels": LANG_LABELS, "lang_dim": LANG_DIM, "lang_hint": LANG_HINT,
            "type_labels": TYPE_LABELS, "type_full_labels": TYPE_FULL_LABELS,
            "subdiv_parent": SUBDIV_PARENT, "subdiv_axis": SUBDIV_AXIS_LABEL,
            "subdiv_hints": SUBDIV_HINTS, "subdiv_defs": SUBDIV_DEFS,
            "scope_rule": SCOPE_RULE,
            "scope": {"modules": [], "groups": [], "langs": [], "empty": True,
                      "effective": {}},
            "scope_summary": [], "lang_summary": [], "preview": None,
            # 「📋 我的问题」（P2b）：未登录/无用户时给空壳，模板照常渲染
            "has_areas": False, "issue_modules": ISSUE_MODULES,
            "issue_labels": ISSUE_LABELS, "issue_icons": ISSUE_ICONS,
            "issue_page_size": ISSUE_PAGE_SIZE, "issue_hint": ISSUE_HINT,
            "full": [], "full_totals": {},
            "full_tabs": [], "full_active": None, "full_tab": "",
            "handled_rows": [], "handled_total": 0, "recheck_run": None}
    # 分页参数（模板里表单隐藏字段 / 分页控件用；未登录时全为第 1 页）
    ctx.update({k: 1 for k in ISSUE_PAGE_ARGS})
    ctx["issue_pages"] = {k: 1 for k in ISSUE_PAGE_ARGS}
    ctx["issue_page_args"] = ISSUE_PAGE_ARGS
    return ctx


def me_context(db, user: dict, tab: str = "", pages: dict | None = None) -> dict:
    """/me 页面渲染关注领域（只读回显）+ 已关注领域文档范围预览 + 我的问题所需上下文；
    预览/问题取数异常不该让页面挂掉。

    tab：问题列表的模块页签（来自 ?ft=，旧 ?dt= 等价）。
    pages：列表三个分区的页码（来自 ?fp/?fi/?fh，旧 ?dp/?di/?dh 当别名，见 page_args）。
    """
    ctx = _empty_context()
    if not user or not user.get("id"):
        return ctx
    uid = int(user["id"])
    ctx["area_options"] = area_options(db)
    ctx["labels"] = _label_map(ctx["area_options"])
    ctx["areas"] = selected_areas(db, uid)
    ctx["areas_eff"] = areas_effective(ctx["areas"])
    ctx["has_legacy"] = any(ctx["areas"].get(d) for d in LEGACY_DIMS)
    ctx["area_total"] = sum(len(v) for v in ctx["areas_eff"].values())
    ctx["scope"] = areas_scope(ctx["areas"])
    ctx["scope_summary"] = scope_summary(ctx["scope"])
    ctx["lang_summary"] = lang_summary(ctx["scope"])
    try:
        ctx["preview"] = scope_preview(db, ctx["scope"])
    except Exception:  # noqa: BLE001 - 预览算不出来时页面降级（不影响其它区）
        ctx["preview"] = None
    try:
        ctx.update(issue_context(db, ctx["areas"], tab, pages))
    except Exception:  # noqa: BLE001 - 问题列表算不出来时页面降级（关注领域区照常）
        pass
    return ctx


def build_me_context(db_path: str, user: dict, tab: str = "",
                     pages: dict | None = None) -> dict:
    """同 me_context，但自行开关连接（脚本/测试用）。"""
    ctx = _empty_context()
    if not user or not user.get("id"):
        return ctx
    db = IndexDB(db_path)
    try:
        return me_context(db, user, tab, pages)
    finally:
        db.close()


# ── 写操作 ─────────────────────────────────────────────────────────────
def _valid_value(options: dict, dim: str, value: str) -> bool:
    """取值必须落在该维度当前可选集合内（防止手改表单塞入脏值）。"""
    return value in {v for v, _l, _c in (options or {}).get(dim, [])}


def _form_name(dim: str) -> str:
    """细分 dim → 表单字段名（kit@guides → kit_guides）。"""
    return dim.replace("@", "_")


def register_me(app, db_path: str = DB_PATH):
    """挂载「关注领域」读写路由（/me 的读页面仍在 auth.py）。"""

    def _require_user(next_path: str = "/me"):
        """返回 (user, redirect_response)；未登录时 user=None 并给出跳转响应。"""
        from auth import auth_enabled, current_user   # 延迟导入：避免与 auth 循环依赖

        user = current_user()
        if user:
            return user, None
        if not auth_enabled():
            return None, redirect("/")
        return None, redirect(f"/auth/login?next={next_path}")

    @me_bp.route("/me/areas", methods=["GET", "POST"], strict_slashes=False)
    def me_areas():
        """「关注领域」独立配置页：①语言 + ②模块 + ③文档范围（大类 + 大类内细分）。

        GET  渲染配置页（选项从 docs 表 DISTINCT 取，已保存的回填）。
        POST 整体替换该用户的配置（user_areas 表，dim = module / type /
             kit@guides / kit@references / ide@guides / lang），
             保存后 302 回本页以便核对回填。
        """
        user, resp = _require_user("/me/areas")
        if resp is not None:
            return resp
        if not user:            # 防御：_require_user 已兜住未登录
            return redirect("/")

        uid = int(user["id"])
        db = IndexDB(db_path)
        try:
            options = area_options(db)
            if request.method == "GET":
                ctx = areas_page_context(db, user)
                return render_template("me_areas.html", current_user=user,
                                       notice=None, **ctx)

            # ── POST：解析表单 → 整体替换 ──
            def _getlist(name: str) -> list:
                return [v.strip() for v in request.form.getlist(name)
                        if v and v.strip()]

            mods = [v for v in _getlist(MODULE_DIM) if v in MODULE_LABELS]
            types = [v for v in _getlist(TYPE_DIM) if v in TYPE_LABELS]
            langs = [v for v in _getlist(LANG_DIM) if v in LANG_LABELS]
            pairs: list = ([(MODULE_DIM, v) for v in mods]
                           + [(TYPE_DIM, v) for v in types]
                           + [(LANG_DIM, v) for v in langs])
            warns: list = []
            n_sub = 0
            for parent, defs in SUBDIV_DEFS.items():
                for dim, axis, _col in defs:
                    vals = _getlist(_form_name(dim))
                    if not vals:
                        continue
                    if parent not in types:   # 大类没勾 → 细分无处安放，丢弃并提示
                        warns.append(f"{TYPE_LABELS[parent]}的{axis}细分已忽略"
                                     f"（未勾选「{TYPE_LABELS[parent]}」）")
                        continue
                    bad = [v for v in vals
                           if len(v) > MAX_VALUE_LEN or not _valid_value(options, dim, v)]
                    if bad:
                        warns.append(f"{TYPE_LABELS[parent]}的{axis}里有"
                                     f"{len(bad)} 个取值不在可选范围内，已忽略")
                    good = [v for v in vals if v not in bad]
                    pairs += [(dim, v) for v in good]
                    n_sub += len(good)

            if db.set_user_areas(uid, pairs, clear_dims=ALL_AREA_DIMS):
                if not pairs:
                    flash("✅ 已保存：未选任何关注领域 = 全部模块 + 全部文档 + 全部语言。", "ok")
                else:
                    flash(f"✅ 关注领域已保存：模块 {len(mods)} 项 · "
                          f"文档范围 {len(types)} 个大类"
                          f"{f'（细分 {n_sub} 项）' if n_sub else ''} · "
                          f"语言 {len(langs)} 项"
                          f"{'（全部语言）' if not langs else ''}。", "ok")
                for w in warns:
                    flash(f"⚠️ {w}", "warn")
            else:
                flash("⚠️ 保存失败（数据库写入异常），请稍后重试。", "warn")
        finally:
            db.close()
        return redirect("/me/areas")

    @me_bp.route("/me/logic", methods=["POST"], strict_slashes=False)
    def me_logic():
        """【已废弃】旧的口径开关（union / intersection）。

        细分范围语义上线后全站只有一种口径，这里保留路由只为兼容旧书签 / 旧表单：
        仍会保存开关值，但不再影响任何取数。
        """
        user, resp = _require_user()
        if resp is not None:
            return resp

        logic = (request.form.get("logic") or "").strip()
        if logic not in IndexDB.AREA_LOGICS:
            flash("⚠️ 未知的统计口径，操作已忽略。", "warn")
            return redirect("/me")
        db = IndexDB(db_path)
        try:
            saved = db.set_area_logic(int(user["id"]), logic)
        finally:
            db.close()
        if saved:
            flash("ℹ️ 口径开关已记录，但已废弃：现在统一按「🎯 关注领域」的文档范围取数"
                  "（见「🎯 关注领域」与「📐 已关注领域文档范围」）。", "info")
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
        - 表单里带 dt / ft（两段当前页签）→ 回跳时保留，用户不会「操作完被弹回第一个模块」。
        - 带 ``X-Requested-With: fetch``（模板劫持表单提交）时回 JSON 而不是 302：
          含该条目**最新状态**与问题目标列表，前端据此同步另一段里同一目标的行 / 按钮 / 计数。
        """
        user, resp = _require_user()
        if resp is not None:
            return resp

        run_id = request.form.get("run_id", type=int)
        item_id = request.form.get("item_id", type=int)
        act = (request.form.get("act") or "").strip()
        who = (user or {}).get("login") or (user or {}).get("name") or ""

        def _fail(msg: str):
            """参数/数据不合法：照旧 flash + 回 /me；fetch 请求回同样的 JSON 结构（ok=false）。"""
            flash(msg, "warn")
            if _is_fetch():
                return _json_state({"ok": False, "act": act, "msg": msg, "status": ""})
            return redirect(_back_to_me())

        # 「③ 已处理」清单里的撤销：直接按 handled 记录 id 恢复（不需要 run/item）
        if act == "unhandle_id":
            hid = request.form.get("handled_id", type=int)
            db = IndexDB(db_path)
            rec: dict | None = None
            ign_after = False
            ok = False
            try:
                if hid:
                    rec = next((r for r in db.list_handled(active_only=True)
                                if r["id"] == hid), None)
                    if rec:   # 恢复前先看该目标是否还被忽略（忽略优先级 > 已处理）
                        rules = ignores.active_map(db)
                        ign_after = ignores.is_ignored(rules, rec["module_key"],
                                                       rec["target"], rec["kind"],
                                                       rec.get("doc_key") or "")
                    ok = db.restore_handled(hid, restored_by=who)
            finally:
                db.close()
            msg = ("↩️ 已撤销该「已处理」记录（重新计入「仍存在」）。" if ok
                   else "ℹ️ 该记录已不是生效中的「已处理」。")
            flash(msg, "ok" if ok else "info")
            if _is_fetch():
                return _json_state({
                    "ok": bool(ok), "act": "unhandle_id", "msg": msg,
                    "module": (rec or {}).get("module_key", ""),
                    "doc_key": (rec or {}).get("doc_key") or "",
                    "targets": [rec["target"]] if rec else [],
                    "status": "ignored" if (ok and ign_after) else "open"})
            return redirect(_back_to_me())

        # act：整条（ignore / unignore / handle / unhandle）+ 与模块页对齐的
        # 「全部忽略 / 恢复全部」（all）与「逐条链接忽略」（toggle:<问题序号>）
        if (act not in ("ignore", "unignore", "handle", "unhandle", "all")
                and not act.startswith("toggle:")) or not run_id or not item_id:
            return _fail("⚠️ 操作参数不完整，已忽略本次操作。")

        db = IndexDB(db_path)
        try:
            row = db._conn.execute(
                "SELECT item_type, detail_json FROM items WHERE id=? AND run_id=?",
                (item_id, run_id)).fetchone()
            if not row:
                return _fail("⚠️ 该条目不存在（可能已被清理），请刷新后重试。")
            try:
                detail = json.loads(row[1] or "{}")
            except Exception:  # noqa: BLE001
                detail = {}
            run = db.get_run(run_id) or {}
            mk = detail.get("module") or run.get("module_key") or ""
            if mk not in ISSUE_MODULES or not ignores.supports(mk):
                return _fail("⚠️ 该条目所属模块不支持忽略 / 已处理。")
            probs = ignores.item_problems(mk, detail, row[0])
            if not probs:
                return _fail("⚠️ 该条目没有可操作的问题项。")
            # 写之前的忽略态（供 JSON 里算「哪些目标的状态真的变了」，前端据此同步两段）
            rules0 = ignores.active_map(db)
            flags0 = [ignores.is_ignored(rules0, mk, p["target"], p["kind"],
                                         p.get("doc_key", "")) for p in probs]

            ip = _client_ip()
            n = 0
            if act == "ignore":
                for p in probs:
                    if ignores._backend(db, mk).add_ignore(
                            mk, p["target"], p["kind"], doc_key="",
                            reason="我的问题页", ip=ip):
                        n += 1
                msg = (f"✅ 已忽略 {n} 个问题（全局生效，可在本条「已忽略」里恢复）。" if n
                       else "ℹ️ 这些问题的忽略已经生效过了。")
            elif act == "unignore":
                for p in probs:
                    n += ignores._backend(db, mk).restore_ignores_for(
                        mk, p["target"], p["kind"], p.get("doc_key", ""), ip=ip)
                msg = (f"↩️ 已恢复 {n} 个忽略（重新计入「仍存在」）。" if n
                       else "ℹ️ 没有可恢复的忽略。")
            elif act == "handle":
                for p in probs:
                    if db.mark_handled(mk, p["target"], p["kind"], doc_key="",
                                       note="我的问题页", created_by=who):
                        n += 1
                msg = (f"✅ 已标记「已处理」{n} 个问题（全局生效，不再计入「仍存在」）。" if n
                       else "ℹ️ 这些问题的「已处理」已经生效过了。")
            elif act == "unhandle":
                for p in probs:
                    n += db.restore_handled_for(mk, p["target"], p["kind"],
                                                p.get("doc_key", ""), restored_by=who)
                msg = (f"↩️ 已撤销「已处理」{n} 个问题（重新计入「仍存在」）。" if n
                       else "ℹ️ 没有可撤销的「已处理」。")
            elif act == "all":
                # 「全部忽略 / 恢复全部」：与模块页 POST /<key>/ignore 的 act=all 同语义 ——
                # 还有没忽略的 → 全忽略；否则 → 全恢复（服务端重算问题列表，不信任前端序号）。
                want = not all(flags0)
                selected = ({ignores.encode_problem(p) for p in probs} if want else set())
                added, restored = ignores.apply_selection(db, mk, probs, selected,
                                                          reason="我的问题页", ip=ip)
                n = added + restored
                msg = (f"✅ 已全部忽略 {added} 个问题（全局生效，可点「↩️ 恢复全部」还原）。"
                       if want and added
                       else (f"↩️ 已恢复全部 {restored} 个忽略（重新计入「仍存在」）。"
                             if restored else "ℹ️ 状态没有变化（可能已被别处改过）。"))
            else:  # toggle:<问题序号>：逐条链接（或逐条问题）忽略 ↔ 恢复
                try:
                    idx = int(act.split(":", 1)[1] or "-1")
                except ValueError:
                    return _fail("⚠️ 问题序号不合法，已忽略本次操作。")
                if not 0 <= idx < len(probs):
                    return _fail("⚠️ 问题序号越界（页面可能已过期），请刷新后重试。")
                want_flags = list(flags0)
                want_flags[idx] = not flags0[idx]     # 只翻转第 idx 条，其余保持
                selected = {ignores.encode_problem(p)
                            for j, p in enumerate(probs) if want_flags[j]}
                added, restored = ignores.apply_selection(db, mk, probs, selected,
                                                          reason="我的问题页", ip=ip)
                n = added + restored
                if not n:
                    msg = "ℹ️ 状态没有变化（可能已被别处改过）。"
                elif added:
                    msg = (f"🚫 已忽略这条{ignores.TARGET_LABEL.get(mk, '问题')}"
                           f"（全局生效，再点一次即可恢复）。")
                else:
                    msg = (f"↩️ 已恢复这条{ignores.TARGET_LABEL.get(mk, '问题')}的忽略"
                           f"（重新计入「仍存在」）。")
            flash(msg, "ok" if n else "info")

            if _is_fetch():
                # 回读该条目此刻的最新状态：前端据此同步**另一段**里同（模块 + 目标）的行 /
                # 按钮 / 计数（两段各自的页签数字、统计卡、卡片徽标也在前端按增量更新）。
                resp = {"ok": bool(n), "act": act, "msg": msg, "module": mk,
                        "doc_key": detail.get("doc_key") or "",
                        "targets": [p["target"] for p in probs],
                        "status": "open", "n_probs": len(probs), "n_open": 0,
                        "n_ignored": 0, "n_handled": 0,
                        "all_ignored": False, "all_handled": False,
                        "problems": [], "changed": []}
                try:
                    probs2, st2 = _problem_state(mk, detail, row[0],
                                                 ignores.active_map(db),
                                                 db.active_handled_map())
                    resp.update({
                        "status": (st2["status"] if st2["status"] in
                                   ("open", "ignored", "handled") else "open"),
                        "targets": [p["target"] for p in probs2],
                        "n_open": st2["n_open"], "n_ignored": st2["n_ignored"],
                        "n_handled": st2["n_handled"],
                        "all_ignored": st2["all_ignored"],
                        "all_handled": st2["all_handled"],
                        # 逐条状态（idx 与模板 .ign-mini 的 data-idx 同口径）+ 本次真变了的目标
                        "problems": [{"idx": i, "ignored": bool(p.get("ignored")),
                                      "handled": bool(p.get("handled"))}
                                     for i, p in enumerate(probs2)],
                        "changed": [p["target"] for i, p in enumerate(probs2)
                                    if bool(p.get("ignored"))
                                    != (flags0[i] if i < len(flags0) else False)]})
                except Exception:  # noqa: BLE001 - 回读失败不影响写操作本身
                    pass
                return _json_state(resp)
        finally:
            db.close()
        # 无 JS 的整页回跳：保留两段页签与页码（dt/ft + dp/fp/di/fi/dh/fh），
        # 否则用户在第 3 页点忽略会被弹回第 1 页 / 第一个模块（看起来像操作没生效）。
        return redirect(_back_to_me())

    app.register_blueprint(me_bp)
    return me_bp
