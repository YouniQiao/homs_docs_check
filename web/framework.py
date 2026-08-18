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

_CJK2_RE = re.compile(r"([\u4e00-\u9fff]{2,})")


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
        val = args.get(key, "all")
        state[key] = val
        if val == "all" or f.get("source") == "sort":
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

    @bp.route("/")
    def task_list():
        db = IndexDB(db_path)
        try:
            runs = db.list_runs(key, limit=100)
            extra = {}
            if module.get("context_provider"):
                extra = module["context_provider"](db)
        finally:
            db.close()
        return render_template("task_list.html", module=module,
                               runs=runs, badge_map=badge_map, **extra)

    @bp.route("/run/<int:run_id>")
    def task_detail(run_id):
        from flask import abort
        db = IndexDB(db_path)
        try:
            run = db.get_run(run_id)
            # run_id 是全局主键，校验是否属于当前模块
            if run and run["module_key"] != key:
                abort(404)
            items = db.get_items(run_id) if run else []
        finally:
            db.close()
        filter_state = {}
        if module.get("filters"):
            items, filter_state = _apply_filters(items, module, request.args)
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
                               items=items_page, badge_map=badge_map,
                               filter_state=filter_state,
                               page=page,
                               total_pages=total_pages,
                               total=total,
                               base_qs=urlencode(q),
                               item_columns=_norm_columns(module))

    @bp.route("/run/<int:run_id>/export")
    def task_export(run_id):
        """导出当前 run（配合当前筛选）为 CSV，Excel 可直接打开。"""
        import csv
        import io

        from flask import abort, send_file

        db = IndexDB(db_path)
        try:
            run = db.get_run(run_id)
            if not run or run["module_key"] != key:
                abort(404)
            items = db.get_items(run_id)
            col_defs = _norm_columns(module)
        finally:
            db.close()
        if module.get("filters") and request.args:
            items, _ = _apply_filters(items, module, request.args)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["类型"] + [lab for _, lab, _ in col_defs])
        for it in items:
            w.writerow([_type_labels(it, module)]
                       + [_cell_text(it, f, rt) for f, _, rt in col_defs])
        # \ufeff BOM：让 Excel 正确识别 UTF-8 中文
        content = "\ufeff" + buf.getvalue()
        return send_file(io.BytesIO(content.encode("utf-8")),
                         mimetype="text/csv; charset=utf-8",
                         as_attachment=True,
                         download_name=f"{key}_run{run_id}.csv")

    app.register_blueprint(bp, url_prefix=f"/{key}")
    return bp
