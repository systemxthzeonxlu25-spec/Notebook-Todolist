"""考试冲刺、学习统计与旧数据库无损升级回归测试。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from domain import exam_countdowns, learning_heatmap, sprint_due

TZ = ZoneInfo("Asia/Shanghai")

# Original stage-three DDL: no completed_at column or activity_logs table.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT,
    exam_date TEXT
);
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    source_key TEXT,
    title TEXT NOT NULL,
    due_at TEXT,
    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, note_id, source_key)
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    source_key TEXT,
    kind TEXT NOT NULL DEFAULT 'qa' CHECK(kind IN ('cloze','qa')),
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0 CHECK(step >= 0),
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, note_id, source_key)
);
CREATE TABLE IF NOT EXISTS review_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    success INTEGER NOT NULL CHECK(success IN (0,1)),
    reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_courses_owner ON courses(owner_id);
CREATE INDEX IF NOT EXISTS idx_schedule_owner ON schedule(owner_id, weekday, start_time);
CREATE INDEX IF NOT EXISTS idx_notes_owner ON notes(owner_id, course_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_todos_owner_due ON todos(owner_id, due_at);
CREATE INDEX IF NOT EXISTS idx_cards_owner_due ON cards(owner_id, due_at);
CREATE INDEX IF NOT EXISTS idx_review_logs_owner ON review_logs(owner_id, reviewed_at);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK(action IN ('login','register')),
    ip_key TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_scope
    ON auth_attempts(action, ip_key, identity_key, attempted_at);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_date ON auth_attempts(attempted_at);
"""


@pytest.fixture
def current():
    return datetime(2026, 10, 12, 12, tzinfo=TZ)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "study.sqlite3"
    db.init_db(path)
    return path


def test_sprint_reserves_two_future_opportunities(current):
    exam = "2026-10-13"
    deadline = datetime(2026, 10, 13, tzinfo=TZ)
    ordinary = current + timedelta(days=15)
    first = datetime.fromisoformat(sprint_due(ordinary.isoformat(), exam, current, first_round=True))
    second = datetime.fromisoformat(sprint_due((first + timedelta(days=15)).isoformat(), exam, first.astimezone(TZ)))
    assert current < first < second < deadline
    assert first == current + timedelta(hours=4)
    assert second == current + timedelta(hours=8)


@pytest.mark.parametrize("exam", [None, "2026-10-11", "2026-10-12"])
def test_past_and_today_exams_keep_normal_interval(current, exam):
    normal = current + timedelta(days=3)
    assert datetime.fromisoformat(sprint_due(normal.isoformat(), exam, current)) == normal


def test_sprint_preserves_already_due_and_earlier_cards(current):
    for hours in (-24, 0, 1):
        ordinary = current + timedelta(hours=hours)
        assert datetime.fromisoformat(sprint_due(ordinary.isoformat(), "2026-10-13", current, first_round=True)) == ordinary


def test_sprint_uses_timezone_across_dst():
    local = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("America/New_York"))
    normal = local + timedelta(days=15)
    first = datetime.fromisoformat(sprint_due(normal.isoformat(), "2026-11-02", local, first_round=True))
    deadline = datetime(2026, 11, 2, tzinfo=local.tzinfo)
    assert (first - local.astimezone(timezone.utc)) == timedelta(hours=37 / 3)
    assert first < deadline


def test_exam_update_only_reschedules_owner_and_course(database, current):
    alice = db.create_course(database, "alice", "数学")
    other = db.create_course(database, "alice", "英语")
    bob = db.create_course(database, "bob", "数学")
    far = (current + timedelta(days=15)).isoformat()
    a = db.create_card(database, "alice", "a", "b", due_at=far, course_id=alice["id"])
    unaffected = db.create_card(database, "alice", "c", "d", due_at=far, course_id=other["id"])
    private = db.create_card(database, "bob", "e", "f", due_at=far, course_id=bob["id"])
    assert db.set_course_exam(database, "bob", alice["id"], "2026-10-13", current) is None
    course = db.set_course_exam(database, "alice", alice["id"], "2026-10-13", current)
    assert course["exam_date"] == "2026-10-13"
    assert datetime.fromisoformat(db.get_card(database, "alice", a["id"])["due_at"]) == current + timedelta(hours=4)
    assert db.get_card(database, "alice", unaffected["id"])["due_at"] == unaffected["due_at"]
    assert db.get_card(database, "bob", private["id"])["due_at"] == private["due_at"]
    db.set_course_exam(database, "alice", alice["id"], None, current)
    assert db.get_course(database, "alice", alice["id"])["exam_date"] is None


def test_review_compresses_even_forgotten_cards_and_keeps_normal_step(database, current):
    course = db.create_course(database, "alice", "数学", exam_date="2026-10-13")
    card = db.create_card(database, "alice", "a", "b", course_id=course["id"])
    reviewed = db.review_card(database, "alice", card["id"], True, reviewed_at=current, tz=TZ)
    assert reviewed["step"] == 1
    assert datetime.fromisoformat(reviewed["due_at"]) == current + timedelta(hours=6)
    forgotten = db.review_card(database, "alice", card["id"], False, reviewed_at=current, tz=TZ)
    assert forgotten["step"] == 0
    assert datetime.fromisoformat(forgotten["due_at"]) == current + timedelta(hours=6)
    after_exam = current + timedelta(days=1)
    ordinary = db.review_card(database, "alice", card["id"], True, reviewed_at=after_exam, tz=TZ)
    assert datetime.fromisoformat(ordinary["due_at"]) == after_exam + timedelta(days=1)


def test_countdowns_exclude_past_and_include_today(current):
    courses = [{"id": index, "name": str(index), "exam_date": value}
               for index, value in enumerate((None, "2026-10-11", "2026-10-12", "2026-10-14"))]
    assert [item["days_left"] for item in exam_countdowns(courses, current)] == [0, 2]


def test_statistics_accuracy_activity_and_ownership(database, current, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: current.astimezone(timezone.utc).isoformat(timespec="seconds"))
    course = db.create_course(database, "alice", "数学")
    db.create_course(database, "alice", "空白课程")
    note = db.create_note(database, "alice", "笔记", "", course_id=course["id"])
    card = db.create_card(database, "alice", "a", "b", note_id=note["id"])
    db.review_card(database, "alice", card["id"], True, reviewed_at=current)
    db.review_card(database, "alice", card["id"], False, reviewed_at=current)
    db.review_card(database, "alice", card["id"], True, reviewed_at=current)
    done = db.create_todo(database, "alice", "写作业", note_id=note["id"])
    db.create_todo(database, "alice", "交作业", course_id=course["id"])
    db.update_todo(database, "alice", done["id"], completed=True)
    db.update_todo(database, "alice", done["id"], completed=True)
    db.update_todo(database, "alice", done["id"], completed=False)
    db.update_todo(database, "alice", done["id"], completed=True)
    db.create_note(database, "bob", "秘密", "")
    stats = db.learning_statistics(database, "alice")
    mathematics, empty = stats["courses"]
    assert mathematics["review_attempts"] == 3
    assert mathematics["review_correct"] == 2
    assert mathematics["review_accuracy"] == pytest.approx(0.6667)
    assert mathematics["todo_completion_rate"] == 0.5
    assert empty["review_accuracy"] is None
    assert empty["todo_completion_rate"] is None
    assert stats["todo_total"] == 2 and stats["todo_completed"] == 1
    assert len(stats["events"]) == 5  # 1 creation + 3 answers + first completion.
    assert len(db.learning_statistics(database, "bob")["events"]) == 1
    assert db.learning_statistics(database, "unknown")["events"] == []
    assert db.learning_statistics(database, "alice", since=current + timedelta(seconds=1))["events"] == []
    assert len(db.learning_statistics(database, "alice", since=current, until=current + timedelta(seconds=1))["events"]) == 5
    db.delete_note(database, "alice", note["id"])
    assert len(db.learning_statistics(database, "alice")["events"]) == 5


def test_heatmap_groups_utc_events_by_app_day(current):
    events = [{"created_at": "2026-10-11T15:59:59+00:00"},
              {"created_at": "2026-10-11T16:00:00+00:00"},
              {"created_at": "2026-10-12T15:59:59+00:00"},
              {"created_at": "2026-10-12T16:00:00+00:00"}]
    assert learning_heatmap(events, current, days=3) == [
        {"date": "2026-10-10", "count": 0},
        {"date": "2026-10-11", "count": 1},
        {"date": "2026-10-12", "count": 2},
    ]


def test_schema_migration_is_idempotent_and_preserves_legacy_data(tmp_path, current):
    path = tmp_path / "legacy.sqlite3"
    timestamp = current.astimezone(timezone.utc).isoformat(timespec="seconds")
    with db._connection(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        note = db._insert(conn, "notes", dict(owner_id="alice", title="旧笔记", content="完整原文", created_at=timestamp, updated_at=timestamp))
        todo = db._insert(conn, "todos", dict(owner_id="alice", title="旧已完成待办", note_id=note["id"], completed=1, created_at=timestamp))
        card = db._insert(conn, "cards", dict(owner_id="alice", front="问题", back="答案", note_id=note["id"], due_at=timestamp, created_at=timestamp))
        db._insert(conn, "review_logs", dict(owner_id="alice", card_id=card["id"], success=1, reviewed_at=timestamp))
    user = db.create_user(path, "Alice", "alice", "legacy-test-hash")
    for _ in range(2):
        db.init_db(path)
        assert db.get_note(path, "alice", note["id"])["content"] == "完整原文"
        saved = db.get_todo(path, "alice", todo["id"])
        assert saved["completed"] is True and saved["completed_at"] is None
        assert db.get_card(path, "alice", card["id"])["back"] == "答案"
        assert db.get_user(path, user["id"])["username"] == "Alice"
        stats = db.learning_statistics(path, "alice")
        assert len(stats["events"]) == 2  # Legacy task has no known completion date.
        assert stats["todo_completed"] == 1


def test_transaction_rollback_does_not_leave_false_activity(database):
    with pytest.raises(ValueError):
        db.create_note(database, "alice", "应回滚", "", todos=[{"title": "完成", "completed": True}, {"title": None}])
    assert db.learning_statistics(database, "alice")["events"] == []


def test_extracted_already_checked_tasks_only_record_once(database):
    note = db.create_note(database, "alice", "导入笔记", "", todos=[{"title": "已完成", "completed": True}])
    db.sync_note_items(database, "alice", note["id"], [{"title": "已完成", "completed": True}], [])
    assert len(db.learning_statistics(database, "alice")["events"]) == 2


def test_moving_note_to_sprint_course_advances_only_its_cards_atomically(database, current):
    old_course = db.create_course(database, "alice", "原课程")
    sprint = db.create_course(database, "alice", "考试课程", exam_date="2026-10-13")
    note = db.create_note(database, "alice", "迁移笔记", "", course_id=old_course["id"])
    far = (current + timedelta(days=15)).isoformat()
    moved = db.create_card(database, "alice", "迁入问题", "答案", due_at=far, note_id=note["id"])
    other = db.create_card(database, "alice", "其他问题", "答案", due_at=far, course_id=sprint["id"])
    with pytest.raises(ValueError):
        db.update_note(database, "alice", note["id"], course_id=sprint["id"], now=current, cards=[{"front": "缺少答案"}])
    assert db.get_note(database, "alice", note["id"])["course_id"] == old_course["id"]
    assert db.get_card(database, "alice", moved["id"])["due_at"] == moved["due_at"]
    db.update_note(database, "alice", note["id"], course_id=sprint["id"], now=current)
    updated = db.get_card(database, "alice", moved["id"])
    assert updated["course_id"] == sprint["id"]
    assert datetime.fromisoformat(updated["due_at"]) == current + timedelta(hours=4)
    assert db.get_card(database, "alice", other["id"])["due_at"] == other["due_at"]
    # Editing unchanged course/content must not keep shortening existing cards.
    db.update_note(database, "alice", note["id"], course_id=sprint["id"], now=current - timedelta(hours=6))
    assert db.get_card(database, "alice", moved["id"])["due_at"] == updated["due_at"]
