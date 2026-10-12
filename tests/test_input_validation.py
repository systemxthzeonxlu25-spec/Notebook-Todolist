"""Malformed security headers and record IDs must not reach internal errors."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from app import create_app


@pytest.fixture
def boundary_app(tmp_path):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "input-boundary-tests-only",
        "DATABASE_PATH": str(tmp_path / "boundaries.sqlite3"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "REGISTER_ENABLED": True,
    })


@pytest.fixture
def signed_client(boundary_app):
    client = boundary_app.test_client()
    token = client.get("/api/session").get_json()["data"]["csrf_token"]
    response = client.post("/api/auth/register", json={"username": "boundary-user", "password": "boundary-test-password"},
                           headers={"X-CSRF-Token": token})
    assert response.status_code == 201
    data = response.get_json()["data"]
    return client, {"X-CSRF-Token": data["csrf_token"]}, data["user"]["id"]


def test_non_ascii_csrf_is_rejected_for_public_and_private_writes(boundary_app, signed_client):
    anonymous = boundary_app.test_client()
    anonymous.get("/api/session")
    client, _, _ = signed_client
    for token in ("é", "中文", "é" * 43, "x" * 10000):
        response = anonymous.post("/api/auth/login", json={"username": "missing", "password": "bad-password"},
                                  headers={"X-CSRF-Token": token})
        assert response.status_code == 403
        response = client.post("/api/notes", json={"title": "must not save"}, headers={"X-CSRF-Token": token})
        assert response.status_code == 403
    assert client.get("/api/notes").get_json()["data"] == []


def test_invalid_route_ids_are_not_found_before_database_access(signed_client):
    client, headers, _ = signed_client
    routes = (
        ("GET", "/api/notes/{id}"), ("PUT", "/api/notes/{id}"), ("DELETE", "/api/notes/{id}"),
        ("GET", "/notes/{id}"), ("PATCH", "/api/courses/{id}"), ("DELETE", "/api/courses/{id}"),
        ("DELETE", "/api/schedule/{id}"), ("PATCH", "/api/todos/{id}"),
        ("POST", "/api/cards/{id}/review"), ("GET", "/attachments/{id}"),
        ("DELETE", "/api/attachments/{id}"), ("GET", "/api/notes/{id}/attachments"),
        ("POST", "/api/notes/{id}/attachments"),
    )
    for bad_id in ("0", "-1", "1.5", str(2**63), "9" * 4301):
        for method, pattern in routes:
            response = client.open(pattern.format(id=bad_id), method=method, json={}, headers=headers)
            assert response.status_code == 404, (method, pattern, len(bad_id), response.status_code)


def test_invalid_association_ids_are_rejected_without_creating_data(signed_client):
    client, headers, _ = signed_client
    for bad_id in (True, False, 0, -1, 1.5, 1.0, float("inf"), float("nan"), 2**63, 10**100, "1.5", "-1", "9" * 4301, [], {}):
        for endpoint, body in (
            ("/api/notes", {"title": "invalid course", "course_id": bad_id}),
            ("/api/schedule", {"course_id": bad_id, "weekday": 0, "start_time": "09:00", "end_time": "10:00"}),
            ("/api/todos", {"items": [{"title": "task"}], "note_id": bad_id}),
            ("/api/todos", {"items": [{"title": "task"}], "course_id": bad_id}),
            ("/api/cards", {"items": [{"front": "question", "back": "answer"}], "note_id": bad_id}),
            ("/api/cards", {"items": [{"front": "question", "back": "answer"}], "course_id": bad_id}),
        ):
            response = client.post(endpoint, json=body, headers=headers)
            assert response.status_code == 400, (endpoint, type(bad_id).__name__, response.status_code)
    assert client.get("/api/notes").get_json()["data"] == []
    assert client.get("/api/todos").get_json()["data"] == []
    assert client.get("/api/cards").get_json()["data"] == []
    for bad_id in ("0", "-1", "1.5", str(2**63), "9" * 4301):
        assert client.get("/api/notes", query_string={"course_id": bad_id}).status_code == 400


def test_valid_integer_ids_and_query_string_ids_still_work(signed_client):
    client, headers, _ = signed_client
    course = client.post("/api/courses", json={"name": "数学"}, headers=headers).get_json()["data"]
    response = client.post("/api/notes", json={"title": "valid", "course_id": str(course["id"])}, headers=headers)
    assert response.status_code == 201
    note = response.get_json()["data"]
    assert note["course_id"] == course["id"]
    assert client.get(f"/api/notes/{note['id']}").status_code == 200
    assert client.get("/api/notes", query_string={"course_id": str(course["id"])}).get_json()["data"][0]["id"] == note["id"]
    assert client.get(f"/api/notes/{2**63 - 1}").status_code == 404


def test_database_rejects_invalid_ids_before_sql_binding(boundary_app, signed_client):
    _, _, owner = signed_client
    path = boundary_app.config["DATABASE_PATH"]
    note = db.create_note(path, owner, "valid", "")
    for bad_id in (True, 0, -1, 1.5, 1.0, float("inf"), 2**63, 10**100, "1", [], {}):
        operations = (
            lambda: db.get_note(path, owner, bad_id),
            lambda: db.delete_note(path, owner, bad_id),
            lambda: db.delete_course(path, owner, bad_id),
            lambda: db.delete_schedule(path, owner, bad_id),
            lambda: db.list_notes(path, owner, course_id=bad_id),
            lambda: db.list_attachments(path, owner, bad_id),
            lambda: db.create_note(path, owner, "bad", "", course_id=bad_id),
            lambda: db.create_manual_todos(path, owner, [{"title": "bad"}], note_id=bad_id),
            lambda: db.create_manual_cards(path, owner, [{"front": "bad", "back": "bad"}], course_id=bad_id),
            lambda: db.update_note(path, owner, note["id"], course_id=bad_id),
        )
        for operation in operations:
            with pytest.raises(ValueError):
                operation()
    assert db.positive_id(2**63 - 1) == 2**63 - 1
    assert db.get_note(path, owner, 2**63 - 1) is None
    assert db.delete_note(path, owner, 2**63 - 1) is False
    assert len(db.list_notes(path, owner)) == 1
