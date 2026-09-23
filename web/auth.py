"""GitCode OAuth 2.0 登录（P1 阶段）。

凭据一律从项目根目录的 .auth.env 读取（600、git-ignored），代码里不写死任何值；
文件缺失或键不齐时整体降级为「登录未配置」——顶栏入口自动隐藏，相关页面给友好提示，
任何情况下都不抛 500。

路由：
  GET /auth/login     生成 state 存 session → 302 到 GitCode 授权页
  GET /auth/callback  校验 state → code 换 token → 取用户 → upsert users → 写 session → 跳 next
  GET /auth/logout    清 session → 回首页
  GET /me             「我的」页（未登录跳登录；登录未配置给提示）

GitCode OAuth 流程（https://docs.gitcode.com/docs/apis/oauth）：
  授权   GET  {AUTHORIZE_URL}?client_id&redirect_uri&response_type=code&scope&state
  换令牌 POST {TOKEN_URL}?grant_type=authorization_code&code&client_id
         client_secret 放 body（form-data），不放 query
  取用户 GET  {USERINFO_URL}   Authorization: Bearer {access_token}
"""

from __future__ import annotations

import secrets
import urllib.parse
from datetime import timedelta
from pathlib import Path

import requests
from flask import (Blueprint, current_app, redirect, render_template, request,
                   session)

from db import IndexDB

BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_ENV_PATH = BASE_DIR / ".auth.env"

# 凭据键（值一律来自 .auth.env，代码里不写死）
_REQUIRED_KEYS = ("GITCODE_CLIENT_ID", "GITCODE_CLIENT_SECRET", "GITCODE_REDIRECT_URI",
                  "GITCODE_AUTHORIZE_URL", "GITCODE_TOKEN_URL", "GITCODE_USERINFO_URL")
# scope 可选（GitCode 文档里无 *）；如需改用 .auth.env 的 GITCODE_SCOPE 覆盖
_DEFAULT_SCOPE = "all_user"
_HTTP_TIMEOUT = 15
_SESSION_DAYS = 30

auth_bp = Blueprint("auth", __name__)


# ── 配置读取 ────────────────────────────────────────────────────────────
def load_auth_env(path=AUTH_ENV_PATH) -> dict:
    """解析 .auth.env（KEY=value，# 注释，值可带引号）。文件不存在/不可读返回 {}。"""
    env: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            env[key] = val
    return env


def auth_env_complete(env: dict) -> bool:
    """凭据是否齐备（决定登录功能开关）。"""
    return all((env or {}).get(k) for k in _REQUIRED_KEYS)


def auth_enabled() -> bool:
    """登录功能是否可用；模板用它决定是否显示入口。无应用上下文时视为未启用。"""
    try:
        return bool(current_app.config.get("AUTH_ENABLED"))
    except RuntimeError:
        return False


def current_user() -> dict | None:
    """当前登录用户（从 session 取）；未登录或未启用返回 None。"""
    if not auth_enabled():
        return None
    try:
        uid = session.get("user_id")
    except RuntimeError:  # 没有 secret_key / 无应用上下文
        return None
    if not uid:
        return None
    return {
        "id": uid,
        "gitcode_id": session.get("gitcode_id"),
        "login": session.get("login"),
        "name": session.get("name") or session.get("login"),
        "avatar_url": session.get("avatar") or "",
    }


# ── 内部工具 ────────────────────────────────────────────────────────────
def _safe_next(target: str | None) -> str:
    """只允许站内相对路径，避免开放重定向。"""
    if not target:
        return "/"
    t = target.strip()
    if not t.startswith("/") or t.startswith("//"):
        return "/"
    return t


def _notice(msg: str, status: int = 200):
    """友好提示页（复用 me.html，保持站点样式），不抛 500。"""
    return render_template("me.html", current_user=None, profile=None,
                           notice=msg), status


def _as_json(resp) -> dict:
    """GitCode 正常返回 JSON；个别情况返回 form 编码，这里都兜住。"""
    try:
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        try:
            return {k: v[0] for k, v in urllib.parse.parse_qs(resp.text).items()}
        except Exception:  # noqa: BLE001 - 解析失败不该影响主流程
            return {}


def _exchange_code(code: str) -> tuple[str | None, str | None]:
    """授权码换 access_token。返回 (token, 错误信息)。"""
    env = current_app.config.get("AUTH_ENV") or {}
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": env["GITCODE_CLIENT_ID"],
    }
    # 文档口径：client_secret 走 body（form-data），不在 query 里
    data = {"client_secret": env["GITCODE_CLIENT_SECRET"]}
    try:
        resp = requests.post(env["GITCODE_TOKEN_URL"], params=params, data=data,
                             headers={"Accept": "application/json"},
                             timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        return None, f"无法连接 GitCode 换取令牌（{type(e).__name__}）"
    if resp.status_code != 200:
        return None, f"换取令牌失败（HTTP {resp.status_code}）"
    token = (_as_json(resp) or {}).get("access_token")
    if not token:
        return None, "GitCode 未返回 access_token"
    return str(token), None


def _fetch_user(token: str) -> tuple[dict | None, str | None]:
    """用 access_token 取当前用户。返回 (profile, 错误信息)。"""
    env = current_app.config.get("AUTH_ENV") or {}
    try:
        resp = requests.get(env["GITCODE_USERINFO_URL"],
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/json"},
                            timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        return None, f"无法获取 GitCode 用户信息（{type(e).__name__}）"
    if resp.status_code != 200:
        return None, f"获取 GitCode 用户信息失败（HTTP {resp.status_code}）"
    payload = _as_json(resp) or {}
    gid = payload.get("id") or payload.get("gitcode_id")
    login = payload.get("login") or payload.get("username")
    if not gid or not login:
        return None, "GitCode 返回的用户信息不完整（缺少 id/login）"
    return {
        "gitcode_id": str(gid),
        "login": str(login),
        "name": payload.get("name") or "",
        "avatar_url": payload.get("avatar_url") or "",
        "email": payload.get("email") or "",
    }, None


# ── 注册 ────────────────────────────────────────────────────────────────
def register_auth(app, db_path: str):
    """挂载 GitCode 登录（蓝图 + session 相关配置）。凭据缺失时自动降级。"""
    env = load_auth_env()
    app.config["AUTH_ENV"] = env
    app.config["AUTH_ENABLED"] = auth_env_complete(env)
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=_SESSION_DAYS))
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    if not app.config["AUTH_ENABLED"]:
        app.logger.warning("GitCode 登录未启用：.auth.env 缺失或凭据不完整（顶栏入口自动隐藏）")

    @app.before_request
    def _session_cookie_secure():
        # nginx 终结 TLS：按 X-Forwarded-Proto 动态决定 cookie 是否带 Secure，
        # 保证 https 下安全、本地 http 调试也能跑通 OAuth 回调。
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        app.config["SESSION_COOKIE_SECURE"] = (proto == "https")

    @auth_bp.route("/auth/login", strict_slashes=False)
    def auth_login():
        if not auth_enabled():
            return _notice("登录功能尚未配置（缺少 .auth.env 凭据），暂时无法使用 GitCode 登录。")
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        session["oauth_next"] = _safe_next(request.args.get("next"))
        query = urllib.parse.urlencode({
            "client_id": env["GITCODE_CLIENT_ID"],
            "redirect_uri": env["GITCODE_REDIRECT_URI"],
            "response_type": "code",
            "scope": env.get("GITCODE_SCOPE") or _DEFAULT_SCOPE,
            "state": state,
        })
        sep = "&" if "?" in env["GITCODE_AUTHORIZE_URL"] else "?"
        return redirect(f"{env['GITCODE_AUTHORIZE_URL']}{sep}{query}")

    @auth_bp.route("/auth/callback", strict_slashes=False)
    def auth_callback():
        if not auth_enabled():
            return _notice("登录功能尚未配置，无法处理授权回调。")
        next_url = _safe_next(session.pop("oauth_next", None))

        err = request.args.get("error") or request.args.get("error_description")
        if err:
            return _notice(f"GitCode 授权未通过：{err}")

        state = request.args.get("state") or ""
        saved = session.pop("oauth_state", None)
        if not saved or not state or not secrets.compare_digest(str(saved), str(state)):
            return _notice("state 校验失败（登录链接可能已过期或被伪造），请重新登录。")

        code = request.args.get("code") or ""
        if not code:
            return _notice("GitCode 未返回授权码（code），请重新登录。")

        token, err = _exchange_code(code)
        if err:
            current_app.logger.warning("GitCode token 交换失败：%s", err)
            return _notice(f"{err}，请稍后重试。")

        profile, err = _fetch_user(token)
        if err:
            current_app.logger.warning("GitCode 用户信息获取失败：%s", err)
            return _notice(f"{err}，请稍后重试。")

        db = IndexDB(db_path)
        try:
            user = db.upsert_user(profile)
        finally:
            db.close()
        if not user:
            return _notice("用户信息写入失败，请稍后重试。")

        session.clear()
        session.permanent = True
        session["user_id"] = int(user["id"])
        session["gitcode_id"] = user["gitcode_id"]
        session["login"] = user["login"]
        session["name"] = user["name"] or user["login"]
        session["avatar"] = user["avatar_url"] or ""
        return redirect(next_url)

    @auth_bp.route("/auth/logout", strict_slashes=False)
    def auth_logout():
        session.clear()
        return redirect("/")

    @auth_bp.route("/me", strict_slashes=False)
    def me():
        user = current_user()
        if not user:
            if not auth_enabled():
                return _notice("登录功能尚未配置，暂时无法查看「我的」页面。")
            return redirect("/auth/login?next=/me")
        profile = None
        if user.get("gitcode_id"):
            db = IndexDB(db_path)
            try:
                profile = db.get_user(user["gitcode_id"])
            finally:
                db.close()
        return render_template("me.html", current_user=user, profile=profile, notice=None)

    app.register_blueprint(auth_bp)
    return auth_bp
