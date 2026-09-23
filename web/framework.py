"""通用「任务-结果」框架。

根据模块定义自动生成两个页面：任务列表（runs）和结果详情（items）。
新增模块只需提供一个模块定义 dict，无需写任何模板/路由。

模块定义结构：
{
    "key": "sync",                          # URL 前缀 /sync
    "name": "文档同步",                      # 导航显示名
    "icon": "📚",
    "description": "华为开发者文档增量同步",
    "summary_fields": [("added", "新增"), ...],   # runs.summary_json 里要展示的字段
    "item_columns": [("title", "标题"), ...],      # items.detail_json 里要展示的列
    "badge_map": {"has_cn": ("badge-modified", "含中文")},  # 可选，item_type 的样式
}
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from flask import Blueprint, render_template, request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402
import ignores  # noqa: E402

# 模块 → 「当前全量问题」对应页签 key（仅复核覆盖的模块）。用于在模块汇总页引导用户跳转。
_RECHECK_TABS = {"ocr": "ocr", "encheck": "encheck", "linkcheck": "linkcheck"}


def _recheck_guide(db, key: str) -> dict:
    """模块汇总页「到当前全量问题看全量问题」的引导上下文（不适用则返回空）。"""
    if key not in _RECHECK_TABS:
        return {}
    try:
        row = db._conn.execute(
            "SELECT id FROM runs WHERE module_key='recheck' AND status='success' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return {"recheck_run_id": row[0], "recheck_tab": _RECHECK_TABS[key]}

_CJK2_RE = re.compile(r"([\u4e00-\u9fff]{2,})")


def _filter_by_show(items: list[dict], mode: str) -> list[dict]:
    """按「显示」模式过滤（忽略只做展示端过滤，run 数据不动）。

    default = 隐藏已被忽略完的项；with = 连已忽略的一起列出；only = 只看含已忽略的项。
    """
    if mode == "only":
        return [it for it in items if it.get("n_ignored", 0) > 0]
    if mode == "with":
        return [it for it in items
                if it.get("n_rem", 0) > 0 or it.get("n_ignored", 0) > 0]
    return [it for it in items if it.get("n_rem", 0) > 0]


def _annotate_ignores(items: list[dict], mk: str, rules: dict) -> None:
    """就地给每条 item 挂忽略信息：problems（含是否已忽略）、被忽略/剩余数。

    每条 item 用**自己所属模块**判定（`detail["module"]`，缺省用 mk）——复核页的
    多个页签混在一起，只有按各自模块判定，才能一次算清各页签的数量。

    **明细一律不裁剪**：被忽略的条目也要照常列出来（灰显 + 标「已忽略」），
    用户才看得到"自己忽略了什么"。行级过滤交给 `_filter_by_show`——
    **只有整行的问题全被忽略时，才隐藏该行**（用户 2026-09 口径）。
    逐条链接的问题（linkcheck 的链接、encheck 的「中文链接」）另挂 link_problems，
    供模板在链接旁边直接渲染开关。
    """
    for it in items:
        mki = it["detail"].get("module") or mk
        if not ignores.supports(mki):
            it["problems"] = []
            it["link_problems"] = {}
            it["n_ignored"], it["n_rem"] = 0, 1
            continue
        fields = {k: f for k, _l, _c, f in ignores.kinds_of(mki) if f}  # kind -> 明细列表字段
        probs = ignores.item_problems(mki, it["detail"], it["item_type"])
        for p in probs:
            p["ignored"] = ignores.is_ignored(rules, mki, p["target"], p["kind"],
                                              p.get("doc_key", ""))
        it["problems"] = probs
        by_field: dict = {}
        for idx, p in enumerate(probs):
            f = fields.get(p["kind"]) if p.get("inline") else None
            if f:
                by_field.setdefault(f, []).append({"idx": idx, "p": p, "link": {}})
        for f, lst in by_field.items():
            raw = it["detail"].get(f) or []
            for i, entry in enumerate(lst):
                entry["link"] = raw[i] if i < len(raw) else {}
        it["link_problems"] = by_field
        _stripped, ign, rem = ignores.strip(mki, it["detail"], rules, it["item_type"])
        it["n_ignored"], it["n_rem"] = ign, rem      # 计数用于「显示」过滤，明细保持原样



def _highlight_cjk(text: str) -> str:
    """高亮文本中的 CJK 片段（<mark> 包裹），用于识别文字里标出中文。"""
    if not text:
        return ""
    return _CJK2_RE.sub(r"<mark>\1</mark>", text)


def _fmt_time(s) -> str:
    """ISO 时间（2026-08-18T17:10:48）转友好格式（2026-08-18 17:10:48）。"""
    if not s:
        return ""
    try:
        return datetime.fromisoformat(str(s)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(s).replace("T", " ")


def _fmt_value(v) -> str:
    """列表值转可读文本（如中文字符列表 -> "中 文 的 ，。"），非列表原样返回。"""
    if isinstance(v, list):
        return " ".join(str(x) for x in v[:30])
    return v

# 忽略相关的「显示」筛选（模块支持忽略时自动插入到筛选区最前）
SHOW_FILTER = {
    "key": "show", "label": "显示", "source": "show",
    "options": [("default", "默认（隐藏整行已忽略）"), ("with", "含已忽略"),
                ("only", "仅已忽略")],
}


def _client_ip() -> str:
    """记录忽略/恢复操作的来源 IP（无鉴权，用留痕便于事后追溯）。"""
    try:
        from flask import request
        x = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "")
        return (x.split(",")[0].strip() or request.remote_addr or "")[:64]
    except Exception:
        return ""


# item_type / status 的默认 badge 样式（css 类, 显示文本）
DEFAULT_BADGE_MAP = {
    "added": ("badge-added", "新增"),
    "modified": ("badge-modified", "修改"),
    "deleted": ("badge-deleted", "删除"),
    "success": ("badge-success", "成功"),
    "failed": ("badge-failed", "失败"),
    "running": ("badge-running", "进行中"),
    "has_cn": ("badge-modified", "含中文"),
    "no_cn": ("badge-added", "无中文"),
}


def _apply_filters(items: list[dict], module: dict, args) -> tuple[list[dict], dict]:
    """按模块定义的 filters 筛选 + 排序。返回 (过滤后的 items, 当前筛选状态)。"""
    state: dict = {}
    for f in module.get("filters", []):
        key = f["key"]
        # text 控件默认空串；select 控件默认 "all"
        default_val = f.get("default", "" if f.get("control") == "text" else "all")
        val = args.get(key, default_val)
        state[key] = val
        skip = (val == "all") or f.get("source") in ("sort", "show")
        if skip:
            continue
        if f["source"] == "item_type":
            items = [it for it in items if it["item_type"] == val]
        elif f["source"] == "count":
            # 计数字段筛选：val -> 字段名映射，>0 即命中；"clean" 全部为 0
            fields = f.get("fields", {})
            if val == "clean":
                fds = list(fields.values())
                items = [it for it in items
                         if all(it["detail"].get(fd, 0) == 0 for fd in fds)]
            elif val == "both":
                # 两者都有：有中文（正文或 URL）且有中文链接（与 badge 判定一致）
                items = [it for it in items
                         if (it["detail"].get("cn_char_count", 0) > 0
                             or it["detail"].get("url_cn_char_count", 0) > 0)
                         and it["detail"].get("cn_link_count", 0) > 0]
            elif val in fields:
                fd = fields[val]
                items = [it for it in items if it["detail"].get(fd, 0) > 0]
        elif f["source"] == "contains":
            # 自由文本子串匹配（如按源文档地址里的任意字段过滤）；留空=全选
            field = f.get("field", f["key"])
            v = str(val or "").strip().lower()
            items = [it for it in items
                     if (not v) or (v in str(it["detail"].get(field, "")).lower())]
        else:
            items = [it for it in items if it["detail"].get(key) == val]
    sort = state.get("sort", "id_desc")
    if sort == "conf_desc":
        items.sort(key=lambda it: it["detail"].get("confidence", 0), reverse=True)
    elif sort == "conf_asc":
        items.sort(key=lambda it: it["detail"].get("confidence", 0))
    elif sort == "int_desc":
        items.sort(key=lambda it: it["detail"].get("int_missing_count", 0), reverse=True)
    elif sort == "ext_desc":
        items.sort(key=lambda it: it["detail"].get("ext_dead_count", 0), reverse=True)
    elif isinstance(sort, str) and (sort.endswith("_desc") or sort.endswith("_asc")) \
            and sort not in ("id_desc", "conf_desc", "conf_asc", "int_desc", "ext_desc"):
        # 通用排序：<detail字段>_desc / <detail字段>_asc
        rev = sort.endswith("_desc")
        fld = sort[:-5] if rev else sort[:-4]
        items.sort(key=lambda it: it["detail"].get(fld, 0) or 0, reverse=rev)
    return items, state


def _norm_columns(module: dict) -> list[tuple]:
    """标准化 item_columns 为 (字段, 标签, 渲染类型) 三元组。

    模块可定义二元组 (f, label) 或三元组 (f, label, render_type)；
    render_type: text / link_list（[{text,url}] 渲染为问题链接列表）等。
    """
    cols = []
    for col in module.get("item_columns", []):
        f, label = col[0], col[1]
        render = col[2] if len(col) > 2 else "text"
        cols.append((f, label, render))
    return cols


def _type_labels(it: dict, module: dict) -> str:
    """类型列文本（multi_badge 拼接标签，否则用 badge_map）。"""
    if module.get("multi_badge"):
        labels = [label for fd, _cls, label in module["multi_badge"]
                  if it["detail"].get(fd, 0) > 0]
        return "、".join(labels) if labels else "正常"
    b = module.get("badge_map", {}).get(it["item_type"], ("", it["item_type"]))
    return b[1]


def _cell_text(it: dict, f: str, render: str) -> str:
    """导出单元格文本（列表 / 链接列表拼接为纯文本）。"""
    if f in ("url", "doc_url"):
        v = it["detail"].get("url") or it["detail"].get("doc_url") or ""
    else:
        v = it["detail"].get(f, "")
    if render == "link_list":
        if isinstance(v, list):
            return " | ".join(
                (f"{l.get('text','')} {l.get('url','')}".strip()) for l in v)
        return ""
    if render == "link":
        return str(v) if v else str(it["detail"].get("target", ""))
    if isinstance(v, list):
        return " ".join(str(x) for x in v[:30])
    return str(v) if v is not None else ""


def register_module(app, db_path: str, module: dict):
    """注册一个任务模块，自动挂载 /<key>/ 和 /<key>/run/<id> 两个路由。"""
    key = module["key"]
    badge_map = {**DEFAULT_BADGE_MAP, **(module.get("badge_map") or {})}
    bp = Blueprint(key, __name__)
    app.jinja_env.filters.setdefault("highlight_cjk", _highlight_cjk)
    app.jinja_env.filters.setdefault("fmt_time", _fmt_time)
    app.jinja_env.filters.setdefault("fmt_value", _fmt_value)

    def _resolve_view(args) -> tuple:
        """解析当前视图配置。模块若定义 tabs（分组页签），则返回当前页签的
        列/徽标/筛选；否则用模块自身的。返回
        (item_columns, badge_map, multi_badge, filters, tabs, active_tab, tab_field)。

        支持忽略的模块（含复核页签对应的源模块）会自动在最前面插入「显示」筛选。
        """
        def _with_show(fs, mk):
            fs = list(fs or [])
            return [SHOW_FILTER] + fs if ignores.supports(mk) else fs

        tabs = module.get("tabs")
        if not tabs:
            return (_norm_columns(module), badge_map, module.get("multi_badge"),
                    _with_show(module.get("filters"), key), None, None, None)
        tab_field = module.get("tab_field", "module")
        tkey = args.get("tab") or tabs[0]["key"]
        active = next((t for t in tabs if t["key"] == tkey), tabs[0])
        bm = {**DEFAULT_BADGE_MAP, **(active.get("badge_map") or {})}
        return (_norm_columns(active), bm, active.get("multi_badge"),
                _with_show(active.get("filters", module.get("filters")), active["key"]),
                tabs, active, tab_field)

    @bp.route("/")
    def task_list():
        db = IndexDB(db_path)
        try:
            runs = db.list_runs(key, limit=100)
            if module.get("runs_provider"):
                runs = module["runs_provider"](db, runs)
            extra = {}
            if module.get("context_provider"):
                extra = module["context_provider"](db)
            extra.update(_recheck_guide(db, key))
        finally:
            db.close()
        return render_template("task_list.html", module=module,
                               runs=runs, badge_map=badge_map, **extra)

    @bp.route("/run/<int:run_id>")
    def task_detail(run_id):
        from flask import abort
        cols, bm, multi_badge, filters, tabs, active_tab, tab_field = _resolve_view(request.args)
        db = IndexDB(db_path)
        try:
            run = db.get_run(run_id)
            # run_id 是全局主键，校验是否属于当前模块
            if run and run["module_key"] != key:
                abort(404)
            if run and module.get("runs_provider"):
                run = module["runs_provider"](db, [run])[0]
            items = db.get_items(run_id) if run else []
            mk = active_tab["key"] if (tabs and active_tab) else key
            has_ign = ignores.supports(mk)
            ig_rules = ignores.active_map(db) if has_ign else {}
        finally:
            db.close()
        # 忽略：只做展示端过滤（run 数据不动 → 恢复即时生效）。
        # 关键：在**页签归组之前**对全部 item 标注/过滤（每条按自己所属模块），
        # 否则只有当前页签算得出数量、其他页签全是 0（用户 2026-09 报的 bug）。
        show_mode = request.args.get("show", "default") if has_ign else "default"
        if show_mode not in ("default", "with", "only"):
            show_mode = "default"
        if has_ign:
            _annotate_ignores(items, mk, ig_rules)
            items = _filter_by_show(items, show_mode)
        # 页签数量（此时已含忽略/显示过滤），随后只保留当前页签的项
        tab_counts: dict = {}
        if tabs and tab_field:
            for it in items:
                tk = it["detail"].get(tab_field)
                tab_counts[tk] = tab_counts.get(tk, 0) + 1
            items = [it for it in items if it["detail"].get(tab_field) == active_tab["key"]]
        filter_state = {}
        if module.get("item_visible") and not tabs and show_mode == "default":
            items = [it for it in items if module["item_visible"](it)]
        if filters:
            items, filter_state = _apply_filters(items, {"filters": filters}, request.args)
        # 分页：每页 per_page（模块可配置，默认 100），在筛选后内存切片
        per_page = module.get("per_page", 100)
        total = len(items)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = request.args.get("page", 1, type=int) or 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        items_page = items[start:start + per_page]
        # 分页链接保留当前筛选参数（去掉 page）
        from urllib.parse import urlencode
        q = {k: v for k, v in request.args.items() if k != "page"}
        return render_template("task_detail.html", module=module, run=run,
                               items=items_page, badge_map=bm, multi_badge=multi_badge,
                               tabs=tabs, active_tab=active_tab, tab_counts=tab_counts,
                               filters=filters,
                               filter_state=filter_state,
                               page=page,
                               total_pages=total_pages,
                               total=total,
                               base_qs=urlencode(q),
                               has_ign=has_ign, ign_module=mk, show_mode=show_mode,
                               item_columns=cols)

    @bp.route("/run/<int:run_id>/export")
    def task_export(run_id):
        """导出当前 run（配合当前筛选）为 CSV，Excel 可直接打开。"""
        import csv
        import io

        from flask import abort, send_file

        cols, bm, multi_badge, filters, tabs, active_tab, tab_field = _resolve_view(request.args)
        view_mod = {**module, "multi_badge": multi_badge, "badge_map": bm}
        # 忽略：导出同样按当前忽略状态与「显示」模式（run 数据不动，恢复即时生效）
        mk = active_tab["key"] if (tabs and active_tab) else key
        has_ign = ignores.supports(mk)
        show_mode = request.args.get("show", "default") if has_ign else "default"
        if show_mode not in ("default", "with", "only"):
            show_mode = "default"
        db = IndexDB(db_path)
        try:
            run = db.get_run(run_id)
            if not run or run["module_key"] != key:
                abort(404)
            items = db.get_items(run_id)
            ig_rules = ignores.active_map(db) if has_ign else {}
            col_defs = cols
        finally:
            db.close()
        if tabs and tab_field:
            items = [it for it in items if it["detail"].get(tab_field) == active_tab["key"]]
        if has_ign:
            _annotate_ignores(items, mk, ig_rules)
            items = _filter_by_show(items, show_mode)
        if module.get("item_visible") and not tabs and show_mode == "default":
            items = [it for it in items if module["item_visible"](it)]
        if filters:
            items, _ = _apply_filters(items, {"filters": filters}, request.args)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow((["类型"] + (["已忽略"] if has_ign else [])
                    + [lab for _, lab, _ in col_defs]))
        for it in items:
            w.writerow([_type_labels(it, view_mod)]
                       + ([it.get("n_ignored", 0)] if has_ign else [])
                       + [_cell_text(it, f, rt) for f, _, rt in col_defs])
        # \ufeff BOM：让 Excel 正确识别 UTF-8 中文
        content = "\ufeff" + buf.getvalue()
        return send_file(io.BytesIO(content.encode("utf-8")),
                         mimetype="text/csv; charset=utf-8",
                         as_attachment=True,
                         download_name=f"{key}_run{run_id}.csv")

    @bp.route("/ignore", methods=["POST"])
    def ignore_action():
        """忽略 / 恢复：`act` = `toggle:<问题序号>`（切换单条）或 `all`（整行全部忽略/恢复）。

        无鉴权（用户决定），故记录来源 IP 留痕；仅接受本站 run 里的条目，
        并以条目自带的 module 字段为准（复核页签复用源模块的条目结构）。
        """
        import json as _json

        from flask import abort, redirect

        run_id = request.form.get("run_id", type=int)
        item_id = request.form.get("item_id", type=int)
        act = (request.form.get("act") or "").strip()
        reason = (request.form.get("reason") or "").strip()[:200]
        db = IndexDB(db_path)
        try:
            run = db.get_run(run_id) if run_id else None
            if not run or run["module_key"] not in (key, "recheck"):
                abort(404)
            row = db._conn.execute(
                "SELECT item_type, detail_json FROM items WHERE id=? AND run_id=?",
                (item_id, run_id)).fetchone()
            if not row:
                abort(404)
            detail = _json.loads(row[1] or "{}")
            mk = detail.get("module") or key
            if not ignores.supports(mk):
                abort(400)
            probs = ignores.item_problems(mk, detail, row[0])
            rules = ignores.active_map(db)
            flags = [ignores.is_ignored(rules, mk, p["target"], p["kind"],
                                        p.get("doc_key", "")) for p in probs]
            if act == "all":
                # 还有没忽略的 → 全部忽略；否则 → 全部恢复
                want = not all(flags)
                selected = {ignores.encode_problem(p) for p in probs} if want else set()
            elif act.startswith("toggle:"):
                i = int(act.split(":", 1)[1] or -1)
                if not 0 <= i < len(probs):
                    abort(400)
                want_flags = list(flags)
                want_flags[i] = not flags[i]          # 只翻转第 i 条，其余保持
                selected = {ignores.encode_problem(p)
                            for j, p in enumerate(probs) if want_flags[j]}
            else:
                abort(400)
            ignores.apply_selection(db, mk, probs, selected, reason=reason,
                                    ip=_client_ip())
            # 回读最新状态：XHR 请求直接原地更新页面（不整页刷新）
            rules2 = ignores.active_map(db)
            flags2 = [ignores.is_ignored(rules2, mk, p["target"], p["kind"],
                                         p.get("doc_key", "")) for p in probs]
            n_ign = sum(1 for f in flags2 if f)
            resp = {"ok": True, "n_ignored": n_ign, "n_rem": len(probs) - n_ign,
                    "problems": [{"idx": i, "ignored": bool(f)}
                                 for i, f in enumerate(flags2)]}
        finally:
            db.close()
        if request.headers.get("X-Requested-With") == "fetch":
            from flask import jsonify
            return jsonify(resp)
        return redirect(request.referrer or f"/{key}/")

    app.register_blueprint(bp, url_prefix=f"/{key}")
    return bp
