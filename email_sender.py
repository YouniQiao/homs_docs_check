"""SMTP 邮件发送（订阅日报）。配置从项目根 .smtp.env 读取。

.smtp.env 内容示例（严格按行, key=value, # 为注释）:
    SMTP_HOST=smtp.exmail.qq.com
    SMTP_PORT=465
    SMTP_USER=noreply@openharmony.cool
    SMTP_PASS=你的SMTP授权码
    MAIL_FROM=noreply@openharmony.cool
"""

from __future__ import annotations

import smtplib
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".smtp.env"
DEFAULT_FROM_NAME = "OpenHarmony 文档检查"


def _load_config() -> dict:
    cfg: dict = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def is_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("SMTP_HOST") and cfg.get("SMTP_USER") and cfg.get("SMTP_PASS"))


def send_mail(to: str, subject: str, body_html: str) -> None:
    cfg = _load_config()
    host = cfg.get("SMTP_HOST")
    port = int(cfg.get("SMTP_PORT") or 465)
    user = cfg.get("SMTP_USER")
    pwd = cfg.get("SMTP_PASS")
    fr = cfg.get("MAIL_FROM") or user
    if not (host and user and pwd):
        raise RuntimeError("SMTP 未配置：请先在 .smtp.env 填写 SMTP_HOST/USER/PASS")

    msg = MIMEText(body_html, "html", "utf-8")
    msg["From"] = formataddr((str(Header(DEFAULT_FROM_NAME, "utf-8")), str(fr)))
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg["X-Mailer"] = "docscheck-subscriber"

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(fr or user, [to], msg.as_string())


def send_bulk(to_list: list[str], subject: str, body_html: str) -> dict:
    """批量发送。返回 {ok: [已发送邮箱], failed: [(邮箱, 错误)]}。"""
    ok, failed = [], []
    for to in to_list:
        try:
            send_mail(to, subject, body_html)
            ok.append(to)
        except Exception as e:  # noqa: BLE001
            failed.append((to, str(e)))
    return {"ok": ok, "failed": failed}


if __name__ == "__main__":
    # 自检：python email_sender.py <测试收件邮箱>
    if len(sys.argv) < 2:
        print("用法: python email_sender.py <收件邮箱>")
        sys.exit(1)
    if not is_configured():
        print("❌ 未配置 .smtp.env")
        sys.exit(1)
    send_mail(sys.argv[1], "OpenHarmony 文档检查 · 邮件通道自检",
              "<h3>✅ 邮件发送通道正常</h3><p>这是 docscheck 订阅系统发出的测试邮件。</p>")
    print("✅ 已发送测试邮件")