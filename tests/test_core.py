"""核心功能的行为与安全回归测试，不依赖外部服务。"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from app import create_app
from domain import current_course, extract_items, next_lesson


@pytest.fixture
def clock():
    return {"now": datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))}


@pytest.fixture
def app(tmp_path, clock):
    test_app = create_app({
        "TESTING": True,
        "SECRET_KEY": "only-for-tests-not-a-production-secret",
        "DATABASE_PATH": str(tmp_path / "notebook.sqlite3"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "TZ": "Asia/Shanghai",
        "NOW_PROVIDER": lambda: clock["now"],
        "REGISTER_ENABLED": True,
    })
    registered_client = test_app.test_client()
    csrf = registered_client.get("/api/session").get_json()["data"]["csrf_token"]
    response = registered_client.post(
        "/api/auth/register",
        json={"username": "core-user", "password": "core-tests-password"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.get_json()
    test_app.config["TEST_OWNER_ID"] = response.get_json()["data"]["user"]["id"]
    test_app.extensions["registered_test_client"] = registered_client
    return test_app


@pytest.fixture
def client(app):
    return app.extensions["registered_test_client"]


@pytest.fixture
def api(client):
    token = client.get("/api/session").get_json()["data"]["csrf_token"]

    def request(method, path, body=None, expected=200):
        response = client.open(path, method=method, json=body, headers={"X-CSRF-Token": token})
        assert response.status_code == expected, response.get_json()
        return response.get_json().get("data")

    return request


def add_course_and_schedule(api):
    course = api("POST", "/api/courses", {"name": "高等数学"}, 201)
    for weekday in (0, 2):
        api("POST", "/api/schedule", {
            "course_id": course["id"], "weekday": weekday,
            "start_time": "09:00", "end_time": "10:00",
        }, 201)
    return course


def test_notes_follow_current_course_and_todos_follow_next_class(api):
    course = add_course_and_schedule(api)
    assert api("GET", "/api/current-course")["course_id"] == course["id"]
    note = api("POST", "/api/notes", {"title": "导数", "content": "- [ ] 写习题\n导数是==变化率=="}, 201)
    assert note["course_id"] == course["id"]
    todo = api("GET", "/api/todos")[0]
    assert todo["note_id"] == note["id"]
    assert todo["due_at"] == "2026-10-14T01:00:00+00:00"
    assert api("GET", "/api/cards")[0]["back"] == "变化率"

    other = api("POST", "/api/courses", {"name": "大学英语"}, 201)
    changed = api("PUT", f"/api/notes/{note['id']}", {"course_id": other["id"]})
    assert changed["course_id"] == other["id"]
    assert api("GET", "/api/todos")[0]["course_id"] == other["id"]
    quick = api("POST", "/api/notes", {"title": "随手记", "course_id": None}, 201)
    assert quick["course_id"] is None


@pytest.mark.parametrize("current,expected", [
    ("2026-10-11T23:59:00+08:00", "2026-10-12T01:00:00+00:00"),
    ("2026-10-12T08:59:59+08:00", "2026-10-12T01:00:00+00:00"),
    ("2026-10-12T09:00:00+08:00", "2026-10-19T01:00:00+00:00"),
    ("2026-10-12T10:00:00+08:00", "2026-10-19T01:00:00+00:00"),
])
def test_next_lesson_week_rollover_and_exact_start(current, expected):
    schedule = [{"course_id": 1, "weekday": 0, "start_time": "09:00", "end_time": "10:00"}]
    assert next_lesson(schedule, 1, datetime.fromisoformat(current)) == expected
    assert next_lesson(schedule, 999, datetime.fromisoformat(current)) is None


def test_current_course_includes_start_excludes_end():
    schedule = [{"course_id": 1, "weekday": 0, "start_time": "09:00", "end_time": "10:00"}]
    assert current_course(schedule, datetime.fromisoformat("2026-10-12T09:00:00+08:00"))["course_id"] == 1
    assert current_course(schedule, datetime.fromisoformat("2026-10-12T10:00:00+08:00")) is None


def test_schedule_rejects_overlaps_but_accepts_adjacent_classes(api):
    course = add_course_and_schedule(api)
    body = {"course_id": course["id"], "weekday": 0, "start_time": "09:30", "end_time": "10:30"}
    api("POST", "/api/schedule", body, 400)
    body.update(start_time="10:00", end_time="11:00")
    api("POST", "/api/schedule", body, 201)
    body.update(start_time="11:00", end_time="10:00")
    api("POST", "/api/schedule", body, 400)


def test_markdown_extracts_tasks_and_multiple_clozes_skips_fences(clock):
    content = """# 课堂笔记
- [ ] 交报告
- [x] 已提交
```python
- [ ] 代码里的任务
==代码常量==
```
~~~text
- [ ] 示例任务
==示例重点==
~~~
函数有==定义域==和==值域==。
"""
    todos, cards = extract_items(content, [], None, clock["now"])
    assert [item["title"] for item in todos] == ["交报告", "已提交"]
    assert [item["completed"] for item in todos] == [False, True]
    assert [item["back"] for item in cards] == ["定义域", "值域"]
    assert all("［填空］" in item["front"] for item in cards)
    assert all(item["due_at"] is None for item in todos)


def test_markdown_fences_require_matching_delimiter_and_length(clock):
    content = """````markdown
```
- [ ] 不应提取一
~~~
==不应提取二==
````
- [ ] 真实任务
"""
    todos, cards = extract_items(content, [], None, clock["now"])
    assert [item["title"] for item in todos] == ["真实任务"]
    assert cards == []


def test_indented_markdown_code_is_not_extracted(clock):
    content = "    - [ ] 代码示例\n    ==代码示例==\n\n- [ ] 真实任务\n==真实重点=="
    todos, cards = extract_items(content, [], None, clock["now"])
    assert [item["title"] for item in todos] == ["真实任务"]
    assert [item["back"] for item in cards] == ["真实重点"]


def test_resaving_preserves_state_and_removal_only_deletes_extracted_items(api, app):
    content = "- [ ] 完成习题\n- [ ] 完成习题\n==定理=="
    note = api("POST", "/api/notes", {"title": "课堂", "content": content}, 201)
    todos = api("GET", "/api/todos")
    assert len(todos) == 2 and todos[0]["source_key"] != todos[1]["source_key"]
    api("PATCH", f"/api/todos/{todos[0]['id']}", {"completed": True, "due_at": "2026-10-20T15:00"})
    card = api("GET", "/api/cards")[0]
    reviewed = api("POST", f"/api/cards/{card['id']}/review", {"remembered": True})
    database = app.config["DATABASE_PATH"]
    manual_todo = db.create_todo(database, app.config["TEST_OWNER_ID"], "手工待办", note_id=note["id"])
    manual_card = db.create_card(database, app.config["TEST_OWNER_ID"], "手工问题", "手工答案", note_id=note["id"])

    api("PUT", f"/api/notes/{note['id']}", {"content": content + "\n补充说明"})
    saved = {item["id"]: item for item in api("GET", "/api/todos")}
    assert saved[todos[0]["id"]]["completed"] is True
    assert saved[todos[0]["id"]]["due_at"] == "2026-10-20T07:00:00+00:00"
    assert saved[todos[1]["id"]]["completed"] is False
    assert db.get_card(database, app.config["TEST_OWNER_ID"], card["id"])["due_at"] == reviewed["due_at"]
    assert db.get_card(database, app.config["TEST_OWNER_ID"], card["id"])["step"] == 1

    api("PUT", f"/api/notes/{note['id']}", {"content": "无派生内容"})
    assert [item["id"] for item in api("GET", "/api/todos")] == [manual_todo["id"]]
    assert [item["id"] for item in api("GET", "/api/cards")] == [manual_card["id"]]
    api("DELETE", f"/api/notes/{note['id']}")
    assert api("GET", "/api/todos") == []
    assert api("GET", "/api/cards") == []


def test_review_intervals_and_forgetting_reset(api, clock):
    api("POST", "/api/notes", {"content": "==重点=="}, 201)
    card = api("GET", "/api/cards")[0]
    for step, days in enumerate((1, 3, 7, 15), start=1):
        reviewed = api("POST", f"/api/cards/{card['id']}/review", {"remembered": True})
        due = datetime.fromisoformat(reviewed["due_at"])
        assert due == clock["now"] + timedelta(days=days)
        assert reviewed["step"] == step
        clock["now"] = due.astimezone(ZoneInfo("Asia/Shanghai"))
    forgotten = api("POST", f"/api/cards/{card['id']}/review", {"remembered": False})
    assert forgotten["step"] == 0
    assert datetime.fromisoformat(forgotten["due_at"]) == clock["now"] + timedelta(days=1)
    next_review = api("POST", f"/api/cards/{card['id']}/review", {"remembered": True})
    assert next_review["step"] == 1
    assert datetime.fromisoformat(next_review["due_at"]) == clock["now"] + timedelta(days=1)


def test_today_uses_local_day_boundary_and_includes_overdue(api, app, clock):
    clock["now"] = datetime(2026, 10, 13, 0, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    database = app.config["DATABASE_PATH"]
    today = db.create_todo(database, app.config["TEST_OWNER_ID"], "今天最后一秒", due_at="2026-10-13T23:59:59+08:00")
    overdue = db.create_todo(database, app.config["TEST_OWNER_ID"], "逾期待办", due_at="2026-10-12T09:00:00+08:00")
    db.create_todo(database, app.config["TEST_OWNER_ID"], "明天第一秒", due_at="2026-10-14T00:00:00+08:00")
    db.create_todo(database, app.config["TEST_OWNER_ID"], "无截止日期")
    done = db.create_todo(database, app.config["TEST_OWNER_ID"], "已完成", due_at="2026-10-13T12:00:00+08:00")
    db.update_todo(database, app.config["TEST_OWNER_ID"], done["id"], completed=True)
    card = db.create_card(database, app.config["TEST_OWNER_ID"], "今天卡片", "答案", due_at="2026-10-13T23:59:59+08:00")
    db.create_card(database, app.config["TEST_OWNER_ID"], "明天卡片", "答案", due_at="2026-10-14T00:00:00+08:00")
    result = api("GET", "/api/today")
    assert result["date"] == "2026-10-13"
    assert {item["id"] for item in result["todos"]} == {today["id"], overdue["id"]}
    assert [item["id"] for item in result["cards"]] == [card["id"]]


def test_search_title_content_course_and_unclassified(api):
    course = api("POST", "/api/courses", {"name": "数学"}, 201)
    titled = api("POST", "/api/notes", {"title": "导数课堂", "content": "求极限", "course_id": course["id"]}, 201)
    body = api("POST", "/api/notes", {"title": "备忘", "content": "导数与概率 100% 下划线_", "course_id": None}, 201)
    assert {item["id"] for item in api("GET", "/api/notes?q=导数")} == {titled["id"], body["id"]}
    assert [item["id"] for item in api("GET", "/api/notes?q=%25")] == [body["id"]]
    assert [item["id"] for item in api("GET", f"/api/notes?course_id={course['id']}")] == [titled["id"]]
    assert [item["id"] for item in api("GET", "/api/notes?unclassified=1")] == [body["id"]]
    assert api("GET", "/api/notes?q=不存在") == []


def test_csrf_requires_session_bound_token_and_read_security_headers(app, client, api):
    assert client.post("/api/courses", json={"name": "恶意请求"}).status_code == 403
    token = client.get("/api/session").get_json()["data"]["csrf_token"]
    assert client.post("/api/courses", json={"name": "恶意请求"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    second_client = app.test_client()
    second_csrf = second_client.get("/api/session").get_json()["data"]["csrf_token"]
    registration = second_client.post(
        "/api/auth/register", json={"username": "second-user", "password": "second-tests-password"},
        headers={"X-CSRF-Token": second_csrf},
    )
    assert registration.status_code == 201
    assert second_client.post("/api/courses", json={"name": "跨会话请求"}, headers={"X-CSRF-Token": token}).status_code == 403
    api("POST", "/api/courses", {"name": "合法请求"}, 201)
    response = client.get("/api/courses")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_database_isolates_owners_and_rejects_cross_owner_links(app):
    database = app.config["DATABASE_PATH"]
    alice_course = db.create_course(database, "alice", "私有课程")
    alice_note = db.create_note(database, "alice", "私有笔记", "私密", alice_course["id"])
    alice_todo = db.create_todo(database, "alice", "私有任务", note_id=alice_note["id"])
    alice_card = db.create_card(database, "alice", "问题", "答案", note_id=alice_note["id"])
    assert db.list_courses(database, "bob") == []
    assert db.list_notes(database, "bob", query="私密") == []
    assert db.list_todos(database, "bob") == []
    assert db.list_cards(database, "bob") == []
    assert db.get_note(database, "bob", alice_note["id"]) is None
    assert db.update_note(database, "bob", alice_note["id"], title="篡改") is None
    assert db.delete_course(database, "bob", alice_course["id"]) is False
    assert db.update_todo(database, "bob", alice_todo["id"], completed=True) is None
    assert db.review_card(database, "bob", alice_card["id"], True) is None
    assert db.sync_note_items(database, "bob", alice_note["id"], [], []) is None
    for operation in (
        lambda: db.create_note(database, "bob", "引用", "", course_id=alice_course["id"]),
        lambda: db.create_todo(database, "bob", "引用", note_id=alice_note["id"]),
        lambda: db.create_card(database, "bob", "引用", "答案", course_id=alice_course["id"]),
        lambda: db.create_schedule(database, "bob", alice_course["id"], 0, "09:00", "10:00"),
    ):
        with pytest.raises(ValueError):
            operation()
    assert db.get_note(database, "alice", alice_note["id"])["title"] == "私有笔记"


def test_invalid_extraction_rolls_back_entire_note_transaction(app):
    database = app.config["DATABASE_PATH"]
    with pytest.raises(ValueError):
        db.create_note(database, app.config["TEST_OWNER_ID"], "应回滚", "", todos=[{"title": "有效"}, {"title": None}])
    assert db.list_notes(database, app.config["TEST_OWNER_ID"]) == []
    assert db.list_todos(database, app.config["TEST_OWNER_ID"]) == []
    note = db.create_note(database, app.config["TEST_OWNER_ID"], "原始标题", "原始内容")
    with pytest.raises(ValueError):
        db.update_note(database, app.config["TEST_OWNER_ID"], note["id"], title="无效修改", cards=[{"front": "缺少答案"}])
    assert db.get_note(database, app.config["TEST_OWNER_ID"], note["id"])["title"] == "原始标题"
