"""HarmonyOS 文档平台 Web 入口。

应用工厂 + 注册所有任务模块。统一端口 3008，各模块挂 /<key>/ 路由前缀。
新增模块只需在 web/modules/ 加一个定义文件并注册到 MODULES 列表。
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, render_template, send_from_directory

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from framework import register_module  # noqa: E402
from modules import MODULES  # noqa: E402
from feedback import register_feedback  # noqa: E402
from subscribe import register_subscribe  # noqa: E402

DB_PATH = str(BASE_DIR / "index.db")


def create_app() -> Flask:
    app = Flask(__name__)

    @app.context_processor
    def inject_nav():
        return {"nav_modules": MODULES}

    @app.route("/media/<path:filepath>")
    def media(filepath: str):
        """serve data/ 下的图片等静态资源（文件路径为 data/ 内的相对路径）。"""
        return send_from_directory(str(BASE_DIR / "data"), filepath)

    for module in MODULES:
        register_module(app, DB_PATH, module)

    register_feedback(app, DB_PATH)
    register_subscribe(app, DB_PATH)

    @app.route("/")
    def home():
        return render_template("home.html")

    @app.after_request
    def no_cache(resp):
        # 动态页面禁用缓存：避免翻页/筛选时浏览器复用旧页面导致内容不变
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3008, debug=False)
