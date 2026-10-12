"""生产配置、代理头信任边界与实际登录限速。"""
from pathlib import Path
import sys

import pytest
from flask import jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app


def config(tmp_path, **overrides):
    return {"TESTING": True, "SECRET_KEY": "proxy-tests-only",
            "DATABASE_PATH": str(tmp_path / "test.sqlite3"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"), **overrides}


@pytest.mark.parametrize("enabled,remote,expected_ip,expected_scheme", [
    (False, "127.0.0.1", "127.0.0.1", "http"),
    (True, "198.51.100.2", "198.51.100.2", "http"),
    (True, "127.0.0.1", "203.0.113.8", "https"),
    (True, "::1", "203.0.113.8", "https"),
])
def test_only_explicitly_trusted_loopback_proxy_changes_ip_and_scheme(tmp_path, enabled, remote, expected_ip, expected_scheme):
    app = create_app(config(tmp_path, TRUST_PROXY=enabled))
    # 使用公开的 healthz 路径验证经过完整 WSGI 中间件后的请求信息。
    app.view_functions["healthz"] = lambda: jsonify(ip=request.remote_addr, scheme=request.scheme, host=request.host)
    response = app.test_client().get("/healthz", headers={
        "X-Forwarded-For": "192.0.2.123, 203.0.113.8",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "attacker.example",
    }, environ_overrides={"REMOTE_ADDR": remote})
    assert response.get_json() == {"ip": expected_ip, "scheme": expected_scheme, "host": "localhost"}


def test_proxy_keeps_login_limits_separate_for_real_clients(tmp_path):
    app = create_app(config(tmp_path, TRUST_PROXY=True, LOGIN_IP_LIMIT=1))
    client = app.test_client()
    token = client.get("/api/session").get_json()["data"]["csrf_token"]

    def login(address, username):
        return client.post("/api/auth/login", json={"username": username, "password": "incorrect-test-password"},
                           headers={"X-CSRF-Token": token, "X-Forwarded-For": address})

    assert login("203.0.113.8", "unknown-one").status_code == 401
    assert login("203.0.113.9", "unknown-two").status_code == 401
    assert login("203.0.113.8", "unknown-three").status_code == 429


@pytest.mark.parametrize("secret", [None, "", "too-short"])
def test_production_rejects_missing_or_short_session_secret(tmp_path, monkeypatch, secret):
    monkeypatch.setenv("APP_ENV", "production")
    if secret is None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SECRET_KEY", secret)
    settings = config(tmp_path, TESTING=False)
    settings.pop("SECRET_KEY")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(settings)


def test_production_uses_secure_cookies_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "production-configuration-test-only" * 2)
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    settings = config(tmp_path, TESTING=False)
    settings.pop("SECRET_KEY")
    app = create_app(settings)
    response = app.test_client().get("/api/session", base_url="https://notes.example.com")
    cookie = response.headers["Set-Cookie"]
    assert all(flag in cookie for flag in ("Secure", "HttpOnly", "SameSite=Lax"))
    assert app.config["TEMPLATES_AUTO_RELOAD"] is False
