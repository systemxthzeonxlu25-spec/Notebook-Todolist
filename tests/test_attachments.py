"""私有图片附件与用户确认的 AI 结果保存测试。"""
from datetime import datetime
from io import BytesIO
from pathlib import Path
import struct
import sys
import uuid
from zoneinfo import ZoneInfo
import zlib

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from app import create_app


@pytest.fixture
def attachment_app(tmp_path):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "attachment-tests-only",
        "DATABASE_PATH": str(tmp_path / "attachments.sqlite3"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "TZ": "Asia/Shanghai",
        "REGISTER_ENABLED": True,
        "NOW_PROVIDER": lambda: datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
    })


def csrf(client):
    return client.get("/api/session").get_json()["data"]["csrf_token"]


def api(client, method, path, body=None, expected=200):
    response = client.open(path, method=method, json=body, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == expected, response.get_json()
    return response.get_json().get("data")


def account(app, username="Alice"):
    client = app.test_client()
    result = api(client, "POST", "/api/auth/register", {"username": username, "password": "attachment-test-password"}, 201)
    return client, result["user"]["id"]


@pytest.fixture
def photo_owner(attachment_app):
    client, owner_id = account(attachment_app)
    note = api(client, "POST", "/api/notes", {"title": "板书笔记", "content": "==重点=="}, 201)
    return client, owner_id, note


def picture(format="JPEG"):
    stream = BytesIO()
    Image.new("RGB", (32, 24), (35, 80, 140)).save(stream, format=format)
    return stream.getvalue()


def upload(client, note_id, content, filename="blackboard.jpg", token=True):
    return client.post(
        f"/api/notes/{note_id}/attachments",
        data={"file": (BytesIO(content), filename)},
        headers={"X-CSRF-Token": csrf(client)} if token else {},
        content_type="multipart/form-data",
    )


@pytest.mark.parametrize("image_format,extension,media_type", [
    ("JPEG", "jpg", "image/jpeg"),
    ("PNG", "png", "image/png"),
    ("WEBP", "webp", "image/webp"),
])
def test_supported_images_preserve_original_bytes_and_private_metadata(attachment_app, photo_owner, image_format, extension, media_type):
    client, owner_id, note = photo_owner
    original = picture(image_format)
    response = upload(client, note["id"], original, f"板书.{extension}")
    assert response.status_code == 201, response.get_json()
    attachment = response.get_json()["data"]
    assert attachment["note_id"] == note["id"]
    metadata = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, attachment["id"])
    assert metadata["owner_id"] == owner_id
    assert "owner_id" not in attachment and "stored_name" not in attachment
    assert attachment["media_type"] == media_type
    assert attachment["size"] == len(original)
    assert attachment["url"] == f"/attachments/{attachment['id']}"
    stored = Path(attachment_app.config["UPLOAD_FOLDER"]) / metadata["stored_name"]
    assert stored.read_bytes() == original
    assert uuid.UUID(stored.stem)
    rendered = client.get(attachment["url"])
    assert rendered.status_code == 200
    assert rendered.mimetype == media_type
    assert rendered.data == original
    assert "no-store" in rendered.headers.get("Cache-Control", "")
    listed = api(client, "GET", f"/api/notes/{note['id']}/attachments")
    assert [item["id"] for item in listed] == [attachment["id"]]


def test_attachments_require_csrf_and_reject_non_image_content(attachment_app, photo_owner):
    client, _, note = photo_owner
    assert upload(client, note["id"], picture(), token=False).status_code == 403
    for invalid in (b"<html><script>alert(1)</script></html>", b"not-an-image", b"", picture()[:40]):
        response = upload(client, note["id"], invalid, "photo.jpg")
        assert response.status_code == 400, response.get_json()
    assert api(client, "GET", f"/api/notes/{note['id']}/attachments") == []
    assert list(Path(attachment_app.config["UPLOAD_FOLDER"]).iterdir()) == []


def test_images_exceeding_byte_or_pixel_limit_are_rejected(attachment_app, photo_owner):
    client, _, note = photo_owner
    too_large = picture() + b"\0" * (12 * 1024 * 1024)
    response = upload(client, note["id"], too_large)
    assert response.status_code in {400, 413}
    # A valid PNG header declaring 30 million pixels exercises the size check
    # without allocating a decompressed image in the test process.
    png = bytearray(picture("PNG"))
    png[16:24] = struct.pack(">II", 6000, 5000)
    png[29:33] = struct.pack(">I", zlib.crc32(png[12:29]))
    response = upload(client, note["id"], bytes(png), "huge.png")
    assert response.status_code == 400, response.get_json()
    assert api(client, "GET", f"/api/notes/{note['id']}/attachments") == []
    assert list(Path(attachment_app.config["UPLOAD_FOLDER"]).iterdir()) == []


def test_attachment_ids_and_note_routes_enforce_user_isolation(attachment_app, photo_owner):
    alice, owner_id, note = photo_owner
    bob, _ = account(attachment_app, "Bobby")
    attachment = upload(alice, note["id"], picture()).get_json()["data"]
    assert bob.get(attachment["url"]).status_code == 404
    api(bob, "GET", f"/api/notes/{note['id']}/attachments", expected=404)
    assert upload(bob, note["id"], picture()).status_code == 404
    api(bob, "DELETE", f"/api/attachments/{attachment['id']}", expected=404)
    anonymous = attachment_app.test_client()
    assert anonymous.get(attachment["url"]).status_code in {302, 401}
    assert alice.get(attachment["url"]).status_code == 200
    metadata = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, attachment["id"])
    stored = Path(attachment_app.config["UPLOAD_FOLDER"]) / metadata["stored_name"]
    assert stored.exists()


def test_attachment_and_note_deletion_remove_database_and_disk_files(attachment_app, photo_owner):
    client, owner_id, note = photo_owner
    first = upload(client, note["id"], picture()).get_json()["data"]
    second = upload(client, note["id"], picture("PNG"), "photo.png").get_json()["data"]
    first = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, first["id"])
    second = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, second["id"])
    directory = Path(attachment_app.config["UPLOAD_FOLDER"])
    unrelated = directory / "unrelated-user-file.txt"
    unrelated.write_text("leave existing files alone")
    api(client, "DELETE", f"/api/attachments/{first['id']}")
    assert not (directory / first["stored_name"]).exists()
    assert db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, first["id"]) is None
    assert (directory / second["stored_name"]).exists()
    api(client, "DELETE", f"/api/notes/{note['id']}")
    assert not (directory / second["stored_name"]).exists()
    assert db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, second["id"]) is None
    assert unrelated.read_text() == "leave existing files alone"


def test_untrusted_upload_filename_cannot_choose_server_path(attachment_app, photo_owner):
    client, owner_id, note = photo_owner
    response = upload(client, note["id"], picture(), "../../outside.jpg")
    assert response.status_code == 201, response.get_json()
    stored_name = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, response.get_json()["data"]["id"])["stored_name"]
    assert "/" not in stored_name and "\\" not in stored_name
    assert uuid.UUID(Path(stored_name).stem)
    assert not (Path(attachment_app.config["UPLOAD_FOLDER"]).parent / "outside.jpg").exists()


@pytest.mark.parametrize("stored_name", ["../image.jpg", "/tmp/image.jpg", "folder\\image.png", "photo.jpg", "..", "bad.svg"])
def test_database_rejects_non_uuid_and_traversal_attachment_names(attachment_app, photo_owner, stored_name):
    _, owner_id, note = photo_owner
    with pytest.raises(ValueError):
        db.create_attachment(attachment_app.config["DATABASE_PATH"], owner_id, note["id"], stored_name, "图片.jpg", "image/jpeg", 100)


def test_manual_ai_results_are_confirmed_atomic_and_survive_note_extraction(attachment_app, photo_owner):
    client, _, note = photo_owner
    todos = api(client, "POST", "/api/todos", {"note_id": note["id"], "items": [
        {"title": "下周提交报告", "due_at": "2026-10-21T23:59:00+08:00"}, {"title": "整理公式"},
    ]}, 201)
    cards = api(client, "POST", "/api/cards", {"note_id": note["id"], "items": [
        {"front": "什么是导数？", "back": "瞬时变化率"},
    ]}, 201)
    assert len(todos) == 2 and len(cards) == 1
    assert all(item["source_key"] is None for item in todos + cards)
    assert cards[0]["kind"] == "qa"
    api(client, "PUT", f"/api/notes/{note['id']}", {"content": "- [ ] 自动待办\n==新重点=="})
    api(client, "PUT", f"/api/notes/{note['id']}", {"content": "移除所有自动提取内容"})
    assert {item["id"] for item in api(client, "GET", "/api/todos")} == {item["id"] for item in todos}
    assert [item["id"] for item in api(client, "GET", "/api/cards")] == [cards[0]["id"]]
    api(client, "POST", "/api/todos", {"note_id": note["id"], "items": [{"title": "先有效"}, {"title": ""}]}, 400)
    api(client, "POST", "/api/cards", {"note_id": note["id"], "items": [{"front": "有效", "back": "答案"}, {"front": "缺答案"}]}, 400)
    assert len(api(client, "GET", "/api/todos")) == 2
    assert len(api(client, "GET", "/api/cards")) == 1


def test_manual_results_cannot_reference_other_users_notes_or_courses(attachment_app, photo_owner):
    alice, _, note = photo_owner
    bob, _ = account(attachment_app, "Bobby")
    course = api(alice, "POST", "/api/courses", {"name": "私有课程"}, 201)
    for resource, items in (("todos", [{"title": "任务"}]), ("cards", [{"front": "问题", "back": "答案"}])):
        for reference in ({"note_id": note["id"]}, {"course_id": course["id"]}):
            api(bob, "POST", f"/api/{resource}", {"items": items, **reference}, 400)
    assert api(bob, "GET", "/api/todos") == []
    assert api(bob, "GET", "/api/cards") == []


def test_failed_metadata_save_removes_only_the_new_upload(attachment_app, photo_owner, monkeypatch):
    client, owner_id, note = photo_owner
    existing = upload(client, note["id"], picture()).get_json()["data"]
    existing_row = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, existing["id"])
    directory = Path(attachment_app.config["UPLOAD_FOLDER"])
    original = (directory / existing_row["stored_name"]).read_bytes()

    def fail_metadata(*args, **kwargs):
        raise ValueError("模拟数据库保存失败")

    monkeypatch.setattr(db, "create_attachment", fail_metadata)
    assert upload(client, note["id"], picture("PNG"), "new.png").status_code == 400
    assert [path.name for path in directory.iterdir()] == [existing_row["stored_name"]]
    assert (directory / existing_row["stored_name"]).read_bytes() == original


def test_uuid_collision_never_removes_existing_original(attachment_app, photo_owner, monkeypatch):
    import uploads

    client, owner_id, note = photo_owner
    original = picture()
    existing = upload(client, note["id"], original).get_json()["data"]
    existing_row = db.get_attachment(attachment_app.config["DATABASE_PATH"], owner_id, existing["id"])
    stored = Path(attachment_app.config["UPLOAD_FOLDER"]) / existing_row["stored_name"]
    collision_id = uuid.UUID(stored.stem)
    monkeypatch.setattr(uploads.uuid, "uuid4", lambda: collision_id)
    with pytest.raises(FileExistsError):
        upload(client, note["id"], original)
    assert stored.read_bytes() == original
    assert len(db.list_attachments(attachment_app.config["DATABASE_PATH"], owner_id, note["id"])) == 1
