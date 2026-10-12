"""注册、会话、数据隔离与持久限速的集成测试。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import uuid

import pytest
from werkzeug.security import check_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from app import create_app


@pytest.fixture
def auth_clock():
    return {"now": datetime(2026, 10, 12, 9, tzinfo=timezone.utc)}


@pytest.fixture
def auth_config(tmp_path, auth_clock):
    return {
        "TESTING": True,
        "SECRET_KEY": "authentication-tests-only",
        "DATABASE_PATH": str(tmp_path / "accounts.sqlite3"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "TZ": "Asia/Shanghai",
        "NOW_PROVIDER": lambda: auth_clock["now"],
        "REGISTER_ENABLED": True,
    }


@pytest.fixture
def auth_app(auth_config):
    return create_app(auth_config)


def session_data(client):
    return client.get("/api/session").get_json()["data"]


def post(client, path, body, expected=200, *, headers=None):
    response = client.post(path, json=body, headers={"X-CSRF-Token": session_data(client)["csrf_token"], **(headers or {})})
    assert response.status_code == expected, response.get_json()
    return response


def register(client, username="Alice", password="safe-test-password"):
    return post(client, "/api/auth/register", {"username": username, "password": password}, 201).get_json()["data"]


def test_registration_hashes_password_assigns_uuid_and_casefolds_username(auth_app):
    client = auth_app.test_client()
    account = register(client)
    user = account["user"]
    assert str(uuid.UUID(user["id"])) == user["id"]
    assert set(user) == {"id", "username"}
    assert user["username"] == "Alice"
    stored = db.get_auth_user(auth_app.config["DATABASE_PATH"], "alice")
    assert stored["password_hash"] != "safe-test-password"
    assert check_password_hash(stored["password_hash"], "safe-test-password")
    assert "password_hash" not in db.get_user(auth_app.config["DATABASE_PATH"], user["id"])
    post(client, "/api/auth/register", {"username": "ALICE", "password": "other-test-password"}, 409)
    post(client, "/api/auth/logout", {})
    result = post(client, "/api/auth/login", {"username": "aLiCe", "password": "safe-test-password"}).get_json()["data"]
    assert result["user"]["id"] == user["id"]


@pytest.mark.parametrize("username,password", [
    ("ab", "valid-password"),
    ("a" * 33, "valid-password"),
    ("has spaces", "valid-password"),
    ("valid", "short"),
    ("valid", "a" * 129),
])
def test_registration_validates_account_fields(auth_app, username, password):
    client = auth_app.test_client()
    post(client, "/api/auth/register", {"username": username, "password": password}, 400)
    assert session_data(client)["user"] is None


def test_private_pages_and_apis_require_login_public_shell_remains_available(auth_app):
    client = auth_app.test_client()
    for path in ("/api/notes", "/api/today", "/api/courses", "/api/schedule", "/api/todos", "/api/cards"):
        assert client.get(path).status_code == 401
    for path in ("/", "/notes", "/notes/new", "/schedule"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/auth?")
    assert client.get("/auth").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/session").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    for path in ("/manifest.webmanifest", "/sw.js", "/offline"):
        assert client.get(path).status_code == 200


def test_csrf_is_required_for_login_and_rotates_with_session(auth_app):
    client = auth_app.test_client()
    original = session_data(client)["csrf_token"]
    body = {"username": "Alice", "password": "safe-test-password"}
    assert client.post("/api/auth/register", json=body).status_code == 403
    account = register(client)
    assert account["csrf_token"] != original
    assert client.post("/api/auth/logout", json={}, headers={"X-CSRF-Token": original}).status_code == 403
    after_logout = post(client, "/api/auth/logout", {}).get_json()["data"]
    assert after_logout["user"] is None
    assert after_logout["csrf_token"] != account["csrf_token"]
    assert client.get("/api/notes").status_code == 401
    assert client.post("/api/auth/login", json=body).status_code == 403
    logged_in = post(client, "/api/auth/login", body).get_json()["data"]
    assert logged_in["csrf_token"] != after_logout["csrf_token"]
    with client.session_transaction() as cookie:
        assert cookie.permanent
        assert cookie["user_id"] == account["user"]["id"]
        assert "password" not in cookie
        assert "password_hash" not in cookie
    assert auth_app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert auth_app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_two_users_cannot_read_or_mutate_each_others_data(auth_app):
    alice, bob = auth_app.test_client(), auth_app.test_client()
    alice_user = register(alice)["user"]
    register(bob, "Bobby")
    course = post(alice, "/api/courses", {"name": "Alice的课程"}, 201).get_json()["data"]
    note = post(alice, "/api/notes", {"title": "保密标题", "content": "- [ ] 保密任务\n==保密重点==", "course_id": course["id"]}, 201).get_json()["data"]
    todo = alice.get("/api/todos").get_json()["data"][0]
    card = alice.get("/api/cards").get_json()["data"][0]
    assert note["owner_id"] == alice_user["id"]
    for resource in ("courses", "notes", "todos", "cards", "schedule"):
        assert bob.get(f"/api/{resource}").get_json()["data"] == []
    assert bob.get(f"/api/notes/{note['id']}").status_code == 404
    assert bob.get(f"/notes/{note['id']}").status_code == 404
    for method, path, body in (
        ("PUT", f"/api/notes/{note['id']}", {"content": "篡改"}),
        ("DELETE", f"/api/notes/{note['id']}", None),
        ("DELETE", f"/api/courses/{course['id']}", None),
        ("PATCH", f"/api/todos/{todo['id']}", {"completed": True}),
        ("POST", f"/api/cards/{card['id']}/review", {"remembered": True}),
    ):
        response = bob.open(path, method=method, json=body, headers={"X-CSRF-Token": session_data(bob)["csrf_token"]})
        assert response.status_code == 404, response.get_json()
    post(bob, "/api/notes", {"title": "越权关联", "course_id": course["id"]}, 400)
    assert alice.get(f"/api/notes/{note['id']}").get_json()["data"]["content"] == note["content"]
    assert alice.get("/api/todos").get_json()["data"][0]["completed"] is False


def test_pre_authentication_local_data_is_not_claimed_by_new_account(auth_app):
    database = auth_app.config["DATABASE_PATH"]
    note = db.create_note(database, "local", "本地早期笔记", "不会自动转移")
    client = auth_app.test_client()
    register(client)
    assert client.get("/api/notes").get_json()["data"] == []
    assert client.get(f"/api/notes/{note['id']}").status_code == 404
    assert db.get_note(database, "local", note["id"]) is not None


def test_login_throttle_survives_app_restart_and_expires(auth_app, auth_config, auth_clock):
    auth_app.config["LOGIN_PAIR_LIMIT"] = 3
    client = auth_app.test_client()
    register(client)
    post(client, "/api/auth/logout", {})
    for _ in range(3):
        response = post(client, "/api/auth/login", {"username": "alice", "password": "incorrect"}, 401)
        assert response.get_json()["error"] == "用户名或密码不正确"
    restarted_config = {**auth_config, "LOGIN_PAIR_LIMIT": 3}
    restarted = create_app(restarted_config).test_client()
    response = post(restarted, "/api/auth/login", {"username": "ALICE", "password": "safe-test-password"}, 429)
    assert int(response.headers["Retry-After"]) > 0
    auth_clock["now"] += timedelta(minutes=16)
    post(restarted, "/api/auth/login", {"username": "ALICE", "password": "safe-test-password"})


def test_ip_login_limit_ignores_spoofed_forwarded_header(auth_app):
    auth_app.config["LOGIN_IP_LIMIT"] = 3
    client = auth_app.test_client()
    for index in range(3):
        post(client, "/api/auth/login", {"username": f"missing{index}", "password": "incorrect"}, 401,
             headers={"X-Forwarded-For": f"192.0.2.{index}"})
    post(client, "/api/auth/login", {"username": "another-name", "password": "incorrect"}, 429,
         headers={"X-Forwarded-For": "192.0.2.250"})


def test_registration_ip_limit_and_cooldown(auth_app, auth_clock):
    auth_app.config["REGISTER_IP_LIMIT"] = 2
    client = auth_app.test_client()
    register(client, "first")
    register(client, "second")
    post(client, "/api/auth/register", {"username": "third", "password": "valid-test-password"}, 429)
    auth_clock["now"] += timedelta(hours=1, seconds=1)
    register(client, "third")


def test_closed_registration_still_allows_hidden_password_cli(auth_app):
    auth_app.config["REGISTER_ENABLED"] = False
    client = auth_app.test_client()
    post(client, "/api/auth/register", {"username": "blocked", "password": "valid-test-password"}, 403)
    runner = auth_app.test_cli_runner()
    assert "--password" not in runner.invoke(args=["create-user", "--help"]).output
    result = runner.invoke(args=["create-user", "--username", "admin"], input="cli-test-password\ncli-test-password\n")
    assert result.exit_code == 0, result.output
    assert "cli-test-password" not in result.output
    post(client, "/api/auth/login", {"username": "admin", "password": "cli-test-password"})


@pytest.mark.parametrize("destination", ["https://example.com", "//example.com", "/\\example.com", "/\nexample.com"])
def test_login_rejects_external_or_malformed_next_urls(auth_app, destination):
    client = auth_app.test_client()
    register(client)
    post(client, "/api/auth/logout", {})
    response = post(client, "/api/auth/login", {"username": "Alice", "password": "safe-test-password", "next": destination})
    assert response.get_json()["data"]["next"] == "/"
