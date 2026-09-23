#!/usr/bin/env python3
"""每周复核周报：汇总最近一次「当前全量问题」结果，邮件发送给所有订阅用户。

用法（配合 cron，建议每周一 00:00 —— 即周日 24:00，紧跟周日 22:00 的复核任务）:
    python3 scripts/send_weekly.py                 # 发送
    python3 scripts/send_weekly.py someone@x.com   # 只发指定邮箱（测试）
    python3 scripts/send_weekly.py --sample out.html   # 只生成 HTML 样例、不发送

与日报的区别（用户要求「一眼看出是周报」）：
  · 主题带【周报】前缀；正文头部用紫色系（日报是蓝色系）+「周报」标签
  · 顶部写明统计周期与「每周日晚发送」
  · 内容为复核口径：复核项 / 已解决 / 仍存在 / 已失效 / 解决率，并按模块拆分
  · 增加「与上周对比」（解决率、仍存在数的升降）
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
from recheck.recheck import MODULE_LABEL  # noqa: E402

DB_PATH = BASE / "index.db"
SITE = "https://docscheck.openharmony.cool"

# 周报配色：紫色系，与日报的蓝色系明显区分
P_MAIN, P_DARK = "#7c3aed", "#5b21b6"
C_RED, C_GREEN, C_GRAY, C_DARK, C_LINK = "#d93025", "#1e8e3e", "#80868b", "#3c4043", "#6d28d9"
MODULES_ORDER = ("linkcheck", "encheck", "ocr")


def latest_recheck(db, before_id: int | None = None) -> tuple | None:
    """取最近一条成功的复核 run：返回 (run_id, started_at, summary, items_by_module)。"""
    q = ("SELECT id, started_at, summary_json FROM runs WHERE module_key='recheck' "
         "AND status='success'")
    args: tuple = ()
    if before_id:
        q += " AND id < ?"
        args = (before_id,)
    q += " ORDER BY id DESC LIMIT 1"
    row = db._conn.execute(q, args).fetchone()
    if not row:
        return None
    run_id, ts, sj = row
    try:
        summary = json.loads(sj or "{}")
    except Exception:
        summary = {}
    return run_id, ts, summary


def _pct(a: int, b: int) -> float:
    return round(a * 100 / b, 1) if b else 0.0


def _delta(now: float, prev: float | None, unit: str = "", invert: bool = False) -> str:
    """升降标注；invert=True 表示「下降是好事」（如仍存在数）。"""
    if prev is None:
        return ""
    d = round(now - prev, 1)
    if abs(d) < 0.05:
        return f'<span style="color:{C_GRAY};font-size:12px">（与上周持平）</span>'
    good = (d < 0) if invert else (d > 0)
    color = C_GREEN if good else C_RED
    arrow = "↑" if d > 0 else "↓"
    return (f'<span style="color:{color};font-size:12px">'
            f'（{arrow}{abs(d):g}{unit}）</span>')


def _stat_cell(value: str, label: str, color: str) -> str:
    return (f'<td width="25%" align="center" style="padding:10px 4px;">'
            f'<div style="font-size:20px;font-weight:700;color:{color}">{value}</div>'
            f'<div style="font-size:12px;color:{C_GRAY};margin-top:2px">{label}</div></td>')


def _module_rows(summary: dict) -> str:
    rows = []
    for mk in MODULES_ORDER:
        total = int(summary.get(f"{mk}_total", 0) or 0)
        if not total:
            continue
        res = int(summary.get(f"{mk}_resolved", 0) or 0)
        st = int(summary.get(f"{mk}_still", 0) or 0)
        gn = int(summary.get(f"{mk}_gone", 0) or 0)
        ig = int(summary.get(f"{mk}_ignored", 0) or 0)
        rate = _pct(res, total - ig)
        rows.append(
            f'<tr>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #f0f2f5;font-size:13px;color:{C_DARK}">'
            f'{MODULE_LABEL.get(mk, mk)}</td>'
            f'<td align="right" style="padding:7px 8px;border-bottom:1px solid #f0f2f5;font-size:13px">{total:,}</td>'
            f'<td align="right" style="padding:7px 8px;border-bottom:1px solid #f0f2f5;font-size:13px;color:{C_GREEN}">{res:,}</td>'
            f'<td align="right" style="padding:7px 8px;border-bottom:1px solid #f0f2f5;font-size:13px;color:{C_RED};font-weight:600">{st:,}</td>'
            f'<td align="right" style="padding:7px 8px;border-bottom:1px solid #f0f2f5;font-size:13px;color:{C_GRAY}">{gn:,}</td>'
            f'<td align="right" style="padding:7px 8px;border-bottom:1px solid #f0f2f5;font-size:13px;font-weight:600">{rate}%</td>'
            f'</tr>')
    head = (
        f'<tr style="background:#faf7ff">'
        f'<th align="left" style="padding:7px 8px;font-size:12px;color:{C_GRAY};font-weight:600">模块</th>'
        f'<th align="right" style="padding:7px 8px;font-size:12px;color:{C_GRAY};font-weight:600">复核项</th>'
        f'<th align="right" style="padding:7px 8px;font-size:12px;color:{C_GRAY};font-weight:600">已解决</th>'
        f'<th align="right" style="padding:7px 8px;font-size:12px;color:{C_GRAY};font-weight:600">仍存在</th>'
        f'<th align="right" style="padding:7px 8px;font-size:12px;color:{C_GRAY};font-weight:600">已失效</th>'
        f'<th align="right" style="padding:7px 8px;font-size:12px;color:{C_GRAY};font-weight:600">解决率</th>'
        f'</tr>')
    return ('<table width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse">' + head + "".join(rows) + "</table>")


def build_html(cur: tuple, prev: tuple | None = None, links: bool = True) -> str:
    """cur/prev = (run_id, started_at, summary)。"""
    run_id, ts, s = cur
    day = (ts or "")[:10]
    total = int(s.get("total", 0) or 0)
    res = int(s.get("resolved", 0) or 0)
    st = int(s.get("still", 0) or 0)
    gn = int(s.get("gone", 0) or 0)
    ig = int(s.get("ignored", 0) or 0)
    rate = float(s.get("rate", 0) or 0)

    ps = (prev[2] if prev else {}) or {}
    p_rate = float(ps.get("rate", 0) or 0) if prev else None
    p_st = int(ps.get("still", 0) or 0) if prev else None

    head_line = (f'<span style="color:{C_GREEN};font-size:16px;font-weight:700">'
                 f'✅ 历史问题已全部处理完毕</span>' if st == 0 else
                 f'<span style="color:{C_RED};font-size:16px;font-weight:700">'
                 f'⚠️ 本周仍有 <span style="font-size:20px">{st:,}</span> 处问题待处理</span>')
    rate_line = (f'<div style="margin-top:6px;font-size:13px;color:{C_DARK}">'
                 f'总体解决率 <b>{rate}%</b> {_delta(rate, p_rate, "%")}'
                 f'　·　仍存在 {st:,} 处 {_delta(st, p_st, " 处", invert=True)}</div>')
    ig_note = (f'<div style="margin-top:4px;font-size:12px;color:{C_GRAY}">'
               f'另有 {ig:,} 项已忽略，不计入解决率分母</div>' if ig else "")

    stats = ('<table width="100%" cellpadding="0" cellspacing="0" '
             'style="margin:14px 0 4px;background:#faf7ff;border-radius:10px">'
             '<tr>'
             + _stat_cell(f"{total:,}", "复核项", P_DARK)
             + _stat_cell(f"{res:,}", "✅ 已解决", C_GREEN)
             + _stat_cell(f"{st:,}", "⚠️ 仍存在", C_RED)
             + _stat_cell(f"{gn:,}", "➖ 已失效", C_GRAY)
             + '</tr></table>')

    if links:
        detail_link = (f'<a href="{SITE}/recheck/run/{run_id}" '
                       f'style="color:{C_LINK};text-decoration:none;font-weight:600">'
                       f'查看复核详情 →</a>')
        unsub = (f'如需退订，请访问 <a href="{SITE}/subscribe" '
                 f'style="color:{C_LINK};text-decoration:none">订阅/退订页</a>')
    else:
        detail_link = f'<span style="color:{C_LINK};font-weight:600">查看复核详情 →</span>'
        unsub = "如需退订，请访问站点订阅/退订页"

    return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px;">
  <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.08);">
    <tr>
      <td style="padding:22px 24px;background:{P_DARK};background-image:linear-gradient(90deg,{P_DARK},{P_MAIN});">
        <div style="font-size:12px;color:rgba(255,255,255,.85);margin-bottom:4px;">
          <span style="display:inline-block;padding:2px 8px;border:1px solid rgba(255,255,255,.6);border-radius:999px;font-size:11px;">周报 · 每周日晚发送</span>
        </div>
        <div style="font-size:18px;font-weight:700;color:#ffffff;">OpenHarmony 文档检查 · 每周复核周报</div>
        <div style="font-size:12px;color:rgba(255,255,255,.8);margin-top:4px;">统计周期：截至 {day}（全量复核历史问题）</div>
      </td>
    </tr>
    <tr><td style="padding:18px 24px 0;">{head_line}{rate_line}{ig_note}</td></tr>
    <tr><td style="padding:0 24px;">{stats}</td></tr>
    <tr><td style="padding:6px 24px 4px;">
      <div style="font-size:13px;font-weight:600;color:#24357d;margin:6px 0;">各模块复核情况</div>
      {_module_rows(s)}
    </td></tr>
    <tr>
      <td style="padding:14px 24px 20px;border-top:1px solid #f0f2f5;">
        <div style="font-size:12px;color:{C_LINK};font-weight:600;">{detail_link}</div>
        <div style="font-size:11px;color:#9aa0a6;margin-top:10px;">本邮件为每周一 00:00 自动发送的复核周报 · 生成 {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
        <div style="font-size:11px;color:#9aa0a6;margin-top:2px;">{unsub}</div>
      </td>
    </tr>
  </table>
</td></tr></table>
</body></html>
"""


def main() -> None:
    args = sys.argv[1:]
    sample_path = None
    if "--sample" in args:
        i = args.index("--sample")
        sample_path = args[i + 1] if len(args) > i + 1 else "weekly_sample.html"
        args = args[:i] + args[i + 2:]
    email_arg = args[0] if args else None

    db = IndexDB(DB_PATH)
    try:
        cur = latest_recheck(db)
        prev = latest_recheck(db, before_id=cur[0]) if cur else None
        subs = [email_arg] if email_arg else db.list_active_subscribers()
    finally:
        db.close()

    if not cur:
        print("❌ 尚无成功的复核 run，跳过")
        return

    html = build_html(cur, prev)
    if sample_path:
        Path(sample_path).write_text(html, encoding="utf-8")
        print(f"✅ 已生成样例：{sample_path}（未发送）")
        return

    if not is_configured():
        print("❌ SMTP 未配置（.smtp.env），跳过发送")
        return
    if not subs:
        print("无收件人，跳过发送")
        return
    day = (cur[1] or "")[:10]
    rate = cur[2].get("rate", 0)
    subject = f"【周报】OpenHarmony 文档检查 · 每周复核周报 {day}（解决率 {rate}%）"
    res = send_bulk(subs, subject, html)
    print(f"发送完成：成功 {len(res['ok'])}，失败 {len(res['failed'])}")
    for to, err in res["failed"]:
        print(f"  ❌ {to}: {err}")


if __name__ == "__main__":
    main()
