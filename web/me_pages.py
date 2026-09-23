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

注意：忽略与「已处理」都是**全局**的，不按用户区分；「我的问题列表」在 P2b，
handled 表与 IndexDB.mark_handled/restore_handled/list_handled 已就位备用，本文件暂不使用。

路由：
  POST /me/areas   action=add|remove  + dim + value → 302 回 /me（flash 提示）
  POST /me/logic   logic=union|intersection        → 302 回 /me（口径开关，只存不算）
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Blueprint, flash, redirect, request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

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


def _empty_context() -> dict:
    return {"areas": {d: [] for d in DIMS}, "area_options": {}, "labels": {},
            "dims": DIMS, "dim_labels": DIM_LABELS, "dim_hints": DIM_HINTS,
            "area_total": 0, "area_logic": "union", "logic_labels": LOGIC_LABELS,
            "logic_short": LOGIC_SHORT, "logic_desc": LOGIC_DESC,
            "logic_updated_at": None, "preview": None}


def me_context(db, user: dict) -> dict:
    """/me 页面渲染关注领域区 + 口径预览区所需上下文（复用已打开的连接）；
    预览/口径异常不该让页面挂掉。"""
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

    app.register_blueprint(me_bp)
    return me_bp
