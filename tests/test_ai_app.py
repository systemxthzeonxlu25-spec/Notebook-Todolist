"""AI routes retain the application's authentication/CSRF/storage boundary."""
import socket
from pathlib import Path

import httpx
import pytest

from app import create_app
import ai
import db


@pytest.fixture
def ai_app(tmp_path, monkeypatch):
    monkeypatch.setattr(ai.socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))])
    def upstream(request):
        return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 1,
            "model": "mock", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "# 整理结果\n- [ ] 新任务"}}]})
    return create_app({"TESTING": True, "SECRET_KEY": "test-only-secret", "REGISTER_ENABLED": True,
        "DATABASE_PATH": str(tmp_path / "data.sqlite3"), "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "AI_HTTP_TRANSPORT_FACTORY": lambda: httpx.MockTransport(upstream)})


def test_ai_requires_login_and_csrf_and_does_not_persist_key_or_result(ai_app):
    client = ai_app.test_client()
    body = {"content": "原始笔记"}
    fake_key = "test-only-request-key-no-real-secret"
    assert client.post("/api/ai/organize", json=body).status_code == 401
    token = client.get("/api/session").json["data"]["csrf_token"]
    registered = client.post("/api/auth/register", json={"username": "ai-tester", "password": "only-test-password"},
                             headers={"X-CSRF-Token": token}).json["data"]
    assert client.post("/api/ai/organize", json=body, headers={"X-AI-Key": fake_key}).status_code == 403
    response = client.post("/api/ai/organize", json=body,
        headers={"X-AI-Key": fake_key, "X-CSRF-Token": registered["csrf_token"]})
    assert response.status_code == 200
    assert "整理结果" in response.json["data"]["content"]
    owner = registered["user"]["id"]
    assert db.list_notes(ai_app.config["DATABASE_PATH"], owner) == []
    assert db.list_todos(ai_app.config["DATABASE_PATH"], owner) == []
    with client.session_transaction() as session:
        assert fake_key not in repr(dict(session))
    for path in Path(ai_app.config["DATABASE_PATH"]).parent.glob("*.sqlite3*"):
        assert fake_key.encode() not in path.read_bytes()
