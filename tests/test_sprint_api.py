"""考试课程、今日倒计时和学习面板的认证 API 回归测试。"""
from datetime import datetime, time, timedelta, timezone

import db
from test_core import api, app, client, clock


def test_exam_patch_accepts_date_only_and_rejects_invalid_date_without_changes(api, app, clock):
    course = api("POST", "/api/courses", {"name": "高等数学"}, 201)
    database, owner = app.config["DATABASE_PATH"], app.config["TEST_OWNER_ID"]
    far = clock["now"] + timedelta(days=15)
    card = db.create_card(database, owner, "定理", "答案", due_at=far, course_id=course["id"])
    result = api("PATCH", f"/api/courses/{course['id']}", {"exam_date": "2026-10-13"})
    assert result["name"] == "高等数学"
    assert result["exam_date"] == "2026-10-13"
    deadline = datetime.combine(clock["now"].date() + timedelta(days=1), time.min, clock["now"].tzinfo)
    due = datetime.fromisoformat(db.get_card(database, owner, card["id"])["due_at"])
    assert clock["now"] < due <= clock["now"] + (deadline - clock["now"]) / 3
    api("PATCH", f"/api/courses/{course['id']}", {"exam_date": "2026-02-30"}, 400)
    assert db.get_course(database, owner, course["id"])["exam_date"] == "2026-10-13"
    assert datetime.fromisoformat(db.get_card(database, owner, card["id"])["due_at"]) == due
    cleared = api("PATCH", f"/api/courses/{course['id']}", {"exam_date": None})
    assert cleared["exam_date"] is None
    api("PATCH", "/api/courses/9999999", {"exam_date": "2026-10-13"}, 404)


def test_today_countdowns_use_local_dates_and_hide_past_exams(api):
    created = []
    for name, exam in (("过去考试", "2026-10-11"), ("今天考试", "2026-10-12"), ("明天考试", "2026-10-13")):
        course = api("POST", "/api/courses", {"name": name}, 201)
        api("PATCH", f"/api/courses/{course['id']}", {"exam_date": exam})
        created.append(course)
    exams = api("GET", "/api/today")["exams"]
    assert [exam["course_id"] for exam in exams] == [created[1]["id"], created[2]["id"]]
    assert [exam["days_left"] for exam in exams] == [0, 1]


def test_review_and_note_reclassification_use_application_timezone(api, app, clock):
    course = api("POST", "/api/courses", {"name": "考试课程"}, 201)
    api("PATCH", f"/api/courses/{course['id']}", {"exam_date": "2026-10-13"})
    note = api("POST", "/api/notes", {"title": "随手记", "course_id": None}, 201)
    database, owner = app.config["DATABASE_PATH"], app.config["TEST_OWNER_ID"]
    card = db.create_card(database, owner, "迁入卡片", "答案", due_at=clock["now"] + timedelta(days=15), note_id=note["id"])
    api("PUT", f"/api/notes/{note['id']}", {"course_id": course["id"]})
    deadline = datetime.combine(clock["now"].date() + timedelta(days=1), time.min, clock["now"].tzinfo)
    first_due = datetime.fromisoformat(db.get_card(database, owner, card["id"])["due_at"])
    assert first_due <= clock["now"] + (deadline - clock["now"]) / 3
    clock["now"] = first_due.astimezone(clock["now"].tzinfo)
    reviewed = api("POST", f"/api/cards/{card['id']}/review", {"remembered": True})
    assert clock["now"] < datetime.fromisoformat(reviewed["due_at"]) < deadline
    assert reviewed["step"] == 1


def test_statistics_reports_real_rates_and_local_midnight_activity(api, app, clock, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: clock["now"].astimezone(timezone.utc).isoformat(timespec="seconds"))
    clock["now"] = clock["now"].replace(day=11, hour=23, minute=59, second=59)
    course = api("POST", "/api/courses", {"name": "数学"}, 201)
    note = api("POST", "/api/notes", {"title": "跨日笔记", "content": "==定理==\n- [ ] 写报告", "course_id": course["id"]}, 201)
    card = api("GET", "/api/cards")[0]
    todo = api("GET", "/api/todos")[0]
    clock["now"] += timedelta(seconds=1)
    api("POST", f"/api/cards/{card['id']}/review", {"remembered": True})
    api("POST", f"/api/cards/{card['id']}/review", {"remembered": False})
    api("PATCH", f"/api/todos/{todo['id']}", {"completed": True})
    api("PATCH", f"/api/todos/{todo['id']}", {"completed": False})
    api("PATCH", f"/api/todos/{todo['id']}", {"completed": True})
    stats = api("GET", "/api/statistics")
    assert stats["timezone"] == app.config["TZ"]
    assert stats["todo_completion_rate"] == 1
    assert stats["todo_total"] == stats["todo_completed"] == 1
    assert stats["courses"][0]["review_accuracy"] == 0.5
    assert stats["courses"][0]["review_attempts"] == 2
    assert len(stats["heatmap"]) == 84
    assert stats["heatmap"][-2:] == [{"date": "2026-10-11", "count": 1}, {"date": "2026-10-12", "count": 3}]
    assert "events" not in stats
    api("DELETE", f"/api/notes/{note['id']}")
    assert api("GET", "/api/statistics")["heatmap"][-1]["count"] == 3


def test_statistics_require_login_and_isolate_accounts(api, app, client):
    private = api("POST", "/api/courses", {"name": "私有课程"}, 201)
    api("PATCH", f"/api/courses/{private['id']}", {"exam_date": "2026-10-13"})
    api("POST", "/api/notes", {"title": "私有笔记", "content": "==答案==", "course_id": private["id"]}, 201)
    guest = app.test_client()
    assert guest.get("/api/statistics").status_code == 401
    assert guest.get("/statistics").status_code == 302
    csrf = guest.get("/api/session").get_json()["data"]["csrf_token"]
    response = guest.post("/api/auth/register", json={"username": "stats-second-user", "password": "stats-test-password"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201
    csrf = response.get_json()["data"]["csrf_token"]
    foreign = guest.patch(f"/api/courses/{private['id']}", json={"exam_date": "2026-12-31"}, headers={"X-CSRF-Token": csrf})
    assert foreign.status_code == 404
    stats = guest.get("/api/statistics").get_json()["data"]
    assert stats["courses"] == []
    assert stats["todo_total"] == 0
    assert stats["todo_completion_rate"] is None
    assert all(day["count"] == 0 for day in stats["heatmap"])
    assert guest.get("/api/today").get_json()["data"]["exams"] == []
    assert client.get("/statistics").status_code == 200
