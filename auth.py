"""Account/session boundary and persistent authentication throttling.

The main application calls authenticate_request() before its CSRF check.
Passwords and user-provided AI keys must never be logged or saved in sessions.
"""
from datetime import datetime, timezone
import hashlib
import os
import secrets
import unicodedata
from urllib.parse import urlsplit

import click
from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db

bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("not-a-real-account-placeholder")
_PUBLIC_ENDPOINTS = {
    "auth.auth_page", "auth.register", "auth.login", "auth.logout",
    "session_info", "healthz", "static",
}
_PUBLIC_PATHS = {"/manifest.webmanifest", "/manifest.json", "/sw.js", "/offline", "/offline.html"}


def _database():
    return current_app.config["DATABASE_PATH"]


def _now():
    provider = current_app.config.get("NOW_PROVIDER")
    return provider() if provider else datetime.now(timezone.utc)


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def public_user(user):
    return {"id": user["id"], "username": user["username"]} if user else None


def normalize_username(value):
    if not isinstance(value, str):
        raise ValueError("用户名须为 3–32 个中文、字母、数字或 _ . - 字符")
    username = unicodedata.normalize("NFKC", value).strip()
    if not 3 <= len(username) <= 32 or any(not (char.isalnum() or char in "_.-") for char in username):
        raise ValueError("用户名须为 3–32 个中文、字母、数字或 _ . - 字符")
    return username, username.casefold()


def _password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise ValueError("密码长度须为 8–128 个字符")
    return value


def safe_next(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "/"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/"
    return value if not parsed.scheme and not parsed.netloc else "/"


def authenticate_request():
    """Set the owner from the signed session; deny anonymous private access."""
    user_id = session.get("user_id")
    g.user = db.get_user(_database(), user_id) if isinstance(user_id, str) else None
    g.owner_id = g.user["id"] if g.user else None
    if user_id and not g.user:
        session.clear()
    if g.user or request.endpoint in _PUBLIC_ENDPOINTS or request.path in _PUBLIC_PATHS:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="请先登录后继续"), 401
    return redirect(url_for("auth.auth_page", next=safe_next(request.full_path.rstrip("?"))))


def _payload():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    return body


def _keys(username_key):
    # X-Forwarded-For is intentionally ignored. Trust proxy headers only through
    # a separately configured reverse-proxy boundary in the main application.
    ip = request.remote_addr or "unknown"
    return hashlib.sha256(ip.encode()).hexdigest(), hashlib.sha256(username_key.encode()).hexdigest()


def _reserve(action, username_key=""):
    ip_key, identity_key = _keys(username_key)
    if action == "login":
        pair_limit = current_app.config["LOGIN_PAIR_LIMIT"]
        ip_limit = current_app.config["LOGIN_IP_LIMIT"]
        window = current_app.config["LOGIN_WINDOW_SECONDS"]
    else:
        pair_limit = ip_limit = current_app.config["REGISTER_IP_LIMIT"]
        window = current_app.config["REGISTER_WINDOW_SECONDS"]
    reservation = db.reserve_auth_attempt(
        _database(), action, ip_key, identity_key, attempted_at=_now(),
        pair_limit=pair_limit, ip_limit=ip_limit, window_seconds=window,
    )
    if reservation["retry_after"]:
        response = jsonify(error="尝试次数过多，请稍后再试")
        response.status_code = 429
        response.headers["Retry-After"] = str(reservation["retry_after"])
        return response
    return None


def _signed_in(user, body, status=200):
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return jsonify(data={"user": public_user(user), "csrf_token": csrf_token(), "next": safe_next(body.get("next"))}), status


@bp.get("/auth")
def auth_page():
    next_path = safe_next(request.args.get("next"))
    if g.user:
        return redirect(next_path if not next_path.startswith("/auth") else "/")
    return render_template("auth.html", registration_enabled=current_app.config["REGISTER_ENABLED"], next_path=next_path)


@bp.post("/api/auth/register")
def register():
    if not current_app.config["REGISTER_ENABLED"]:
        return jsonify(error="当前站点未开放注册，请联系站点管理员"), 403
    body = _payload()
    limited = _reserve("register")
    if limited:
        return limited
    username, username_key = normalize_username(body.get("username"))
    password = _password(body.get("password"))
    user = db.create_user(_database(), username, username_key, generate_password_hash(password))
    if not user:
        return jsonify(error="注册失败，请更换用户名或稍后重试"), 409
    return _signed_in(user, body, 201)


@bp.post("/api/auth/login")
def login():
    body = _payload()
    username = body.get("username")
    password = body.get("password")
    username_key = unicodedata.normalize("NFKC", username[:128]).strip().casefold() if isinstance(username, str) else "<invalid>"
    limited = _reserve("login", username_key)
    if limited:
        return limited
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 128 or len(password) > 128:
        return jsonify(error="用户名或密码不正确"), 401
    user = db.get_auth_user(_database(), username_key)
    valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    if not user or not valid:
        return jsonify(error="用户名或密码不正确"), 401
    db.clear_login_attempts(_database(), *_keys(username_key))
    return _signed_in(user, body)


@bp.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify(data={"user": None, "csrf_token": csrf_token()})


def init_app(app):
    """Register routes/CLI. Call authenticate_request in the app's request hook."""
    app.config.setdefault("REGISTER_ENABLED", os.getenv("REGISTER_ENABLED", "true").lower() == "true")
    app.config.setdefault("LOGIN_PAIR_LIMIT", 5)
    app.config.setdefault("LOGIN_IP_LIMIT", 30)
    app.config.setdefault("LOGIN_WINDOW_SECONDS", 900)
    app.config.setdefault("REGISTER_IP_LIMIT", 5)
    app.config.setdefault("REGISTER_WINDOW_SECONDS", 3600)
    app.register_blueprint(bp)

    @app.cli.command("create-user")
    @click.option("--username", prompt="用户名", help="用户名；密码始终通过隐藏提示输入。")
    def create_user_command(username):
        """创建用户（关闭公开注册时也可使用）。"""
        password = click.prompt("密码", hide_input=True, confirmation_prompt="再次输入密码")
        try:
            username, username_key = normalize_username(username)
            password = _password(password)
            user = db.create_user(app.config["DATABASE_PATH"], username, username_key, generate_password_hash(password))
        except ValueError as error:
            raise click.ClickException(str(error)) from None
        if not user:
            raise click.ClickException("创建失败，请更换用户名后重试")
        click.echo("用户已创建")
