#!/usr/bin/env python3
"""每日订阅日报：汇总今日各模块检查结果，邮件发送给所有订阅用户。

用法（配合 cron，如每天早上检查完成后执行）:
    python3 scripts/send_daily.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
for _p in (str(BASE), str(BASE / "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import IndexDB  # noqa: E402
from email_sender import is_configured, send_bulk  # noqa: E402
from modules import MODULES  # noqa: E402

import ignores  # noqa: E402

DB_PATH = BASE / "index.db"
SITE = "https://docscheck.openharmony.cool"

# ---- 各模块展示规则：focus = 需重点关注的问题字段(field, label)，全字段例行展示 ----
DETAILS = {
    "sync": {
        "title": "文档同步",
        "mode": "changes",  # 变更型：中性提示，非问题
        "fields": [("added", "新增"), ("modified", "修改"), ("deleted", "删除")],
        "focus": [("added", "新增"), ("modified", "修改"), ("deleted", "删除")],
        "zero_note": "今日无文档变更",
    },
    "encheck": {
        "title": "英文文档检查",
        "fields": [("total", "检查文档"), ("hanzi", "含汉字"),
                   ("cn_link", "含中文链接"), ("url_cn", "链接URL含中文")],
        "focus": [("hanzi", "含汉字"), ("cn_link", "含中文链接"),
                  ("url_cn", "链接URL含中文")],
        "zero_note": "英文文档检查正常",
    },
    "ocr": {
        "title": "图片 OCR 中文检查",
        "fields": [("total", "检查总数"), ("en_has_cn", "英文图含中文")],
        "focus": [("en_has_cn", "英文图含中文")],
        "zero_note": "OCR 全部正常",
    },
    "linkcheck": {
        "title": "链接健康检查",
        "fields": [("checked", "检查文档"), ("dead", "真死链"),
                   ("vintage", "误链历史版本"), ("anchor_miss", "锚点失效")],
        "focus": [("dead", "真死链"), ("vintage", "误链历史版本"),
                  ("anchor_miss", "锚点失效")],
        "zero_note": "链接检查正常",
    },
}

# summary_fields 的补充显示字段（模块已声明但未标为关注项）也例行展示
C_RED, C_GREEN, C_GRAY, C_BLUE, C_DARK = "#d93025", "#1e8e3e", "#80868b", "#1a73e8", "#3c4043"

LABELS = {m["key"]: {f: l for f, l in m.get("summary_fields", [])}
          for m in MODULES}
FOCUS_FIELDS = {mk: {f for f, _ in det.get("focus", [])}
                for mk, det in DETAILS.items()}
MODE = {mk: det.get("mode") for mk, det in DETAILS.items()}


def runs_by_date(db, day: str | None = None) -> list[tuple]:
    """指定日期（YYYY-MM-DD）各模块最新一条 run；day=None 用今天。
    返回 [(module_key, started_at, status, summary, run_id)]。"""
    if day:
        q = ("SELECT id, module_key, started_at, status, summary_json FROM runs "
             "WHERE date(started_at) = ? ORDER BY module_key, id DESC")
        args = (day,)
    else:
        q = ("SELECT id, module_key, started_at, status, summary_json FROM runs "
             "WHERE date(started_at) = date('now','localtime') "
             "ORDER BY module_key, id DESC")
        args = ()
    seen, out = set(), []
    for run_id, mk, ts, status, summary in db._conn.execute(q, args):
        if mk in seen:
            continue
        seen.add(mk)
        out.append((mk, ts, status, json.loads(summary) if summary else {}, run_id))
    return out


def today_runs(db) -> list[tuple]:
    return runs_by_date(db, None)


def _issue_count(mk: str, s: dict) -> int:
    return sum(int(s.get(f, 0) or 0) for f in FOCUS_FIELDS.get(mk, set()))


# 支持忽略的模块：日报里的数字按当前忽略状态重算（已忽略不计入）
_IGNORABLE = ("encheck", "ocr", "linkcheck")


def _recount_run(db, mk: str, run_id: int, rules: dict) -> tuple[dict, int]:
    """按忽略状态重算该 run 的关注字段；返回 (字段值, 已忽略数)。口径同站点概览卡。"""
    rows = db._conn.execute(
        "SELECT item_type, detail_json FROM items WHERE run_id=? ORDER BY id",
        (run_id,)).fetchall()
    items = []
    for it_type, dj in rows:
        try:
            items.append((it_type, json.loads(dj) if dj else {}))
        except Exception:
            pass
    if mk == "linkcheck":
        out = {"dead": 0, "vintage": 0, "anchor_miss": 0}
        ign = 0
        for _t, d in items:
            d2, i2, _r = ignores.strip("linkcheck", d, rules)
            out["dead"] += d2.get("dead_count", 0)
            out["vintage"] += d2.get("vintage_count", 0)
            out["anchor_miss"] += d2.get("anchor_miss_count", 0)
            ign += i2
        return out, ign
    if mk == "encheck":
        latest: dict = {}
        for _t, d in items:
            if d.get("doc_key"):
                latest[d["doc_key"]] = d
        out = {"hanzi": 0, "punct": 0, "url_cn": 0, "cn_link": 0}
        ign = 0
        for d in latest.values():
            d2, i2, _r = ignores.strip("encheck", d, rules)
            for f, key in (("hanzi_count", "hanzi"), ("punct_count", "punct"),
                           ("url_cn_char_count", "url_cn"), ("cn_link_count", "cn_link")):
                if d2.get(f, 0) > 0:
                    out[key] += 1
            ign += i2
        return out, ign
    if mk == "ocr":
        seen: set = set()
        en_has_cn = ign = 0
        for it_type, d in items:
            img = d.get("image")
            if not img or img in seen:
                continue
            seen.add(img)
            if d.get("lang") != "en":
                continue
            d2, i2, _r = ignores.strip("ocr", d, rules, it_type)
            if d2.get("has_cn"):
                en_has_cn += 1
            ign += i2
        return {"en_has_cn": en_has_cn}, ign
    return {}, 0


def adjust_runs_for_ignores(db, runs: list[tuple]) -> list[tuple]:
    """把各模块的关注字段按当前忽略状态重算，并记录忽略数（供卡片提示）。"""
    rules = ignores.active_map(db)
    out = []
    for mk, ts, st, s, rid in runs:
        s = dict(s or {})
        if mk in _IGNORABLE:
            fields, ign = _recount_run(db, mk, rid, rules)
            s.update(fields)
            s["_ignored"] = ign
        out.append((mk, ts, st, s, rid))
    return out


def _module_card(mk: str, ts: str, status: str, s: dict, run_id: int,
                 show_link: bool = True) -> str:
    if mk not in DETAILS:
        return ""
    det = DETAILS[mk]
    name = next((m.get("icon", "") + " " + m["name"] for m in MODULES
                 if m["key"] == mk), mk)
    ok = status == "success"
    badge = (f'<span style="color:{C_GREEN}">● 成功</span>' if ok
             else f'<span style="color:{C_RED}">● 失败</span>')
    field_rows = det.get("fields") or list(LABELS.get(mk, {}).items())

    # 全字段例行展示（含 0）
    rows = []
    for f, lab in field_rows:
        v = int(s.get(f, 0) or 0)
        color = C_DARK
        if v > 0:
            if f in FOCUS_FIELDS.get(mk, set()):
                color = C_RED
            elif MODE.get(mk) == "changes":
                color = C_BLUE
        rows.append(
            f'<tr><td style="padding:4px 0;color:{C_GRAY};font-size:13px">{lab}</td>'
            f'<td align="right" style="padding:4px 0;color:{color};'
            f'font-weight:600;font-size:13px">{v}</td></tr>')
    table = ("<table width='100%' cellpadding='0' cellspacing='0'>"
             + "".join(rows) + "</table>")

    # 状态提示行（关注项汇总）+ 当日结果链接
    issues = _issue_count(mk, s)
    ign = int(s.get("_ignored", 0) or 0)
    ig_note = (f'<span style="color:{C_GRAY};font-size:12px;">（另有 {ign} 处已忽略）</span>'
               if ign else "")
    if MODE.get(mk) == "changes":
        note = (f'<span style="color:{C_GRAY}">{det["zero_note"]}</span>'
                if issues == 0 else "")
    else:
        note = (f'<span style="color:{C_GREEN};font-weight:600">✓ {det["zero_note"]}</span>'
                if issues == 0 else
                f'<span style="color:{C_RED};font-weight:600">⚠ 有 {issues} 处需关注</span>')
    if ig_note:
        note = (note + " " + ig_note) if note else ig_note
    if show_link:
        link = (f'<a href="{SITE}/{mk}/run/{run_id}" '
                f'style="font-size:12px;color:#2f54d0;text-decoration:none;font-weight:600">'
                f'查看当日结果 →</a>')
    else:
        link = ('<span style="font-size:12px;color:#2f54d0;font-weight:600;">'
                '查看当日结果 →</span>')

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:10px 0;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;">
      <tr>
        <td style="padding:8px 18px 4px;border-bottom:1px solid #f0f2f5;">
          <span style="font-size:14px;font-weight:600;color:#24357d;">{name}</span>
          <span style="font-size:12px;color:#9aa0a6;margin-left:10px;">{ts[11:19]} {badge}</span>
        </td>
      </tr>
      <tr>
        <td style="padding:10px 18px 2px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="font-size:13px;">{note}</td>
              <td align="right" style="font-size:12px;white-space:nowrap;">{link}</td>
            </tr>
          </table>
        </td>
      </tr>
      <tr><td style="padding:0px 22px 12px;font-size:13px;">{table}</td></tr>
    </table>
    """


ORDER = ("sync", "encheck", "ocr", "linkcheck")


def build_html(runs: list[tuple], on_dt: datetime | None = None,
               links: bool = True) -> str:
    today = (on_dt or datetime.now()).strftime("%Y-%m-%d")
    runs = sorted(runs, key=lambda r: ORDER.index(r[0]) if r[0] in ORDER else len(ORDER))
    # 顶部"今日发现 N 处需关注"只统计"问题型"模块；文档同步(changes)是变更提示，不计入
    issues = sum(_issue_count(mk, s) for mk, _ts, _st, s, _rid in runs
                 if MODE.get(mk) != "changes")
    overview = (f'<span style="color:{C_GREEN};font-size:15px;font-weight:700;">'
                f'✅ 今日检查全部正常</span>'
                if issues == 0 else
                f'<span style="color:{C_RED};font-size:15px;font-weight:700;">'
                f'⚠️ 今日发现 <span style="font-size:18px">{issues}</span> 处需关注问题</span>')

    mods = "".join(_module_card(mk, ts, st, s, rid, show_link=links)
               for mk, ts, st, s, rid in runs)
    if not runs:
        mods = "<p style='color:#80868b'>今日尚未有检查记录产出。</p>"
    unsub = (f'如需退订，请访问 <a href="{SITE}/subscribe" '
             f'style="color:#2f54d0;text-decoration:none;">订阅/退订页</a>' if links
             else '如需退订，请访问站点订阅/退订页')

    return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px;">
  <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.08);">
    <tr>
      <td style="padding:22px 24px;background:#2f54d0;background-image:linear-gradient(90deg,#24357d,#2f54d0);">
        <div style="font-size:18px;font-weight:700;color:#ffffff;">OpenHarmony 文档检查日报</div>
        <div style="font-size:12px;color:rgba(255,255,255,.8);margin-top:4px;">{today} · 每日定时检查结果汇总</div>
      </td>
    </tr>
    <tr><td style="padding:18px 24px 4px;">{overview}</td></tr>
    <tr><td style="padding:2px 24px 10px;">{mods}</td></tr>
    <tr>
      <td style="padding:14px 24px 20px;border-top:1px solid #f0f2f5;">
        <div style="font-size:11px;color:#9aa0a6;">本邮件由 OpenHarmony 文档检查自动发送 · 生成 {datetime.now().strftime("%H:%M")}</div>
        <div style="font-size:11px;color:#9aa0a6;margin-top:2px;">{unsub}</div>
      </td>
    </tr>
  </table>
</td></tr></table>
</body></html>
"""


def main() -> None:
    if not is_configured():
        print("❌ SMTP 未配置（.smtp.env），跳过发送")
        return
    email_arg = sys.argv[1] if len(sys.argv) > 1 else None
    date_arg = sys.argv[2] if len(sys.argv) > 2 else None
    on = datetime.strptime(date_arg, "%Y-%m-%d") if date_arg else None

    db = IndexDB(DB_PATH)
    try:
        runs = runs_by_date(db, date_arg)
        runs = adjust_runs_for_ignores(db, runs)   # 已忽略的不计入日报数字
        subs = [email_arg] if email_arg else db.list_active_subscribers()
    finally:
        db.close()

    # 测试模式常用 9-11 之类有内容的数据；无订阅或发空时不打扰
    if not subs:
        print("无收件人，跳过发送")
        return
    subject = (f"OpenHarmony 文档检查日报 "
               f"{(on or datetime.now()).strftime('%Y-%m-%d')}")
    res = send_bulk(subs, subject, build_html(runs, on_dt=on))
    print(f"发送完成：成功 {len(res['ok'])}，失败 {len(res['failed'])}")
    for to, err in res["failed"]:
        print(f"  ❌ {to}: {err}")


if __name__ == "__main__":
    main()