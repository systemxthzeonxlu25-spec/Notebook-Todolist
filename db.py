"""SQLite persistence. All application SQL lives in this module.

Every data operation is scoped to an owner. Dates are stored as UTC ISO 8601
strings so SQLite's lexical comparisons have the same order as timestamps.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sqlite3


SCHEMA = """
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
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value, *, nullable=True):
    if value is None or value == "":
        return None if nullable else _now()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("日期时间格式无效") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("日期时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _text(value, label, *, empty=False, max_length=2000000):
    if not isinstance(value, str) or len(value) > max_length or (not empty and not value.strip()):
        raise ValueError(f"{label}无效")
    return value if empty else value.strip()


def _owner(owner_id):
    return _text(owner_id, "用户标识", max_length=128)


def _boolean(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError("布尔值无效")


def _exam_date(value):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("考试日期格式无效") from exc


def _color(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("课程颜色必须是六位十六进制颜色")
    return value


@contextmanager
def _connection(db_path):
    connection = sqlite3.connect(str(db_path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(db_path):
    """Create an empty database without modifying an existing database's data."""
    if str(db_path) != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    with _connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def _get(conn, table, owner_id, item_id):
    row = conn.execute(f"SELECT * FROM {table} WHERE owner_id=? AND id=?", (_owner(owner_id), item_id)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if "completed" in result:
        result["completed"] = bool(result["completed"])
    return result


def _require_reference(conn, table, owner_id, item_id):
    if item_id is not None and not _get(conn, table, owner_id, item_id):
        raise ValueError("关联数据不存在或无权访问")


def _insert(conn, table, fields):
    keys = list(fields)
    cursor = conn.execute(
        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
        [fields[key] for key in keys],
    )
    return _get(conn, table, fields["owner_id"], cursor.lastrowid)


def _update(conn, table, owner_id, item_id, fields):
    if not _get(conn, table, owner_id, item_id):
        return None
    if fields:
        conn.execute(
            f"UPDATE {table} SET {','.join(key + '=?' for key in fields)} WHERE owner_id=? AND id=?",
            [*fields.values(), owner_id, item_id],
        )
    return _get(conn, table, owner_id, item_id)


def _delete(db_path, table, owner_id, item_id):
    with _connection(db_path) as conn:
        return conn.execute(f"DELETE FROM {table} WHERE owner_id=? AND id=?", (_owner(owner_id), item_id)).rowcount > 0


def _check_fields(fields, allowed):
    if set(fields) - set(allowed):
        raise ValueError("包含不可修改的字段")


def list_courses(db_path, owner_id):
    with _connection(db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM courses WHERE owner_id=? ORDER BY id", (_owner(owner_id),))]


def get_course(db_path, owner_id, course_id):
    with _connection(db_path) as conn:
        return _get(conn, "courses", owner_id, course_id)


def create_course(db_path, owner_id, name, color=None, exam_date=None):
    with _connection(db_path) as conn:
        return _insert(conn, "courses", dict(owner_id=_owner(owner_id), name=_text(name, "课程名", max_length=200), color=_color(color), exam_date=_exam_date(exam_date)))


def update_course(db_path, owner_id, course_id, **fields):
    _check_fields(fields, ("name", "color", "exam_date"))
    if "name" in fields:
        fields["name"] = _text(fields["name"], "课程名", max_length=200)
    if "color" in fields:
        fields["color"] = _color(fields["color"])
    if "exam_date" in fields:
        fields["exam_date"] = _exam_date(fields["exam_date"])
    with _connection(db_path) as conn:
        return _update(conn, "courses", owner_id, course_id, fields)


def delete_course(db_path, owner_id, course_id):
    return _delete(db_path, "courses", owner_id, course_id)


def list_schedule(db_path, owner_id):
    with _connection(db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT s.*, c.name AS course_name FROM schedule s JOIN courses c ON c.id=s.course_id AND c.owner_id=s.owner_id WHERE s.owner_id=? ORDER BY s.weekday,s.start_time,s.id", (_owner(owner_id),))]


def create_schedule(db_path, owner_id, course_id, weekday, start_time, end_time):
    if isinstance(weekday, bool) or not isinstance(weekday, int) or weekday not in range(7):
        raise ValueError("星期必须是 0 到 6 的整数")
    for value in (start_time, end_time):
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("上课时间格式必须是 HH:MM")
    if end_time <= start_time:
        raise ValueError("下课时间必须晚于上课时间")
    if course_id is None:
        raise ValueError("课表必须关联课程")
    with _connection(db_path) as conn:
        _require_reference(conn, "courses", owner_id, course_id)
        return _insert(conn, "schedule", dict(owner_id=_owner(owner_id), course_id=course_id, weekday=weekday, start_time=start_time, end_time=end_time))


def delete_schedule(db_path, owner_id, schedule_id):
    return _delete(db_path, "schedule", owner_id, schedule_id)


def list_notes(db_path, owner_id, query="", course_id=None, unclassified=False):
    query = _text(query, "搜索内容", empty=True, max_length=2000)
    clauses, params = ["n.owner_id=?"], [_owner(owner_id)]
    if query:
        clauses.append("(n.title LIKE ? ESCAPE '\\' OR n.content LIKE ? ESCAPE '\\')")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += [f"%{escaped}%", f"%{escaped}%"]
    if unclassified:
        clauses.append("n.course_id IS NULL")
    elif course_id is not None:
        clauses.append("n.course_id=?")
        params.append(course_id)
    with _connection(db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT n.*,c.name AS course_name FROM notes n LEFT JOIN courses c ON c.id=n.course_id AND c.owner_id=n.owner_id WHERE " + " AND ".join(clauses) + " ORDER BY n.updated_at DESC,n.id DESC", params)]


def get_note(db_path, owner_id, note_id):
    with _connection(db_path) as conn:
        return _get(conn, "notes", owner_id, note_id)


def create_note(db_path, owner_id, title, content, course_id=None, *, todos=None, cards=None):
    with _connection(db_path) as conn:
        _require_reference(conn, "courses", owner_id, course_id)
        now = _now()
        note = _insert(conn, "notes", dict(owner_id=_owner(owner_id), title=_text(title, "笔记标题", max_length=500), content=_text(content, "笔记内容", empty=True), course_id=course_id, created_at=now, updated_at=now))
        _sync_note_items(conn, owner_id, note, todos, cards)
        return note


def update_note(db_path, owner_id, note_id, *, todos=None, cards=None, **fields):
    _check_fields(fields, ("title", "content", "course_id"))
    if "title" in fields:
        fields["title"] = _text(fields["title"], "笔记标题", max_length=500)
    if "content" in fields:
        fields["content"] = _text(fields["content"], "笔记内容", empty=True)
    with _connection(db_path) as conn:
        if not _get(conn, "notes", owner_id, note_id):
            return None
        _require_reference(conn, "courses", owner_id, fields.get("course_id"))
        fields["updated_at"] = _now()
        note = _update(conn, "notes", owner_id, note_id, fields)
        if "course_id" in fields:
            # Manual and extracted items follow their source note's course.
            for table in ("todos", "cards"):
                conn.execute(f"UPDATE {table} SET course_id=? WHERE owner_id=? AND note_id=?", (fields["course_id"], owner_id, note_id))
        _sync_note_items(conn, owner_id, note, todos, cards)
        return note


def delete_note(db_path, owner_id, note_id):
    return _delete(db_path, "notes", owner_id, note_id)


def _list_due(db_path, table, owner_id, due_before):
    condition = "t.owner_id=?"
    params = [_owner(owner_id)]
    if due_before is not None:
        condition += " AND t.due_at<=?"
        params.append(_timestamp(due_before, nullable=False))
    with _connection(db_path) as conn:
        rows = conn.execute(f"SELECT t.*,n.title AS note_title,c.name AS course_name FROM {table} t LEFT JOIN notes n ON n.id=t.note_id AND n.owner_id=t.owner_id LEFT JOIN courses c ON c.id=t.course_id AND c.owner_id=t.owner_id WHERE {condition} ORDER BY t.due_at IS NULL,t.due_at,t.id", params)
        result = [dict(row) for row in rows]
        if table == "todos":
            for item in result:
                item["completed"] = bool(item["completed"])
        return result


def list_todos(db_path, owner_id, due_before=None):
    return _list_due(db_path, "todos", owner_id, due_before)


def get_todo(db_path, owner_id, todo_id):
    with _connection(db_path) as conn:
        return _get(conn, "todos", owner_id, todo_id)


def _validate_links(conn, owner_id, note_id, course_id):
    _require_reference(conn, "notes", owner_id, note_id)
    _require_reference(conn, "courses", owner_id, course_id)
    if note_id is not None and course_id is None:
        course_id = _get(conn, "notes", owner_id, note_id)["course_id"]
    return course_id


def create_todo(db_path, owner_id, title, due_at=None, note_id=None, course_id=None, source_key=None):
    with _connection(db_path) as conn:
        course_id = _validate_links(conn, owner_id, note_id, course_id)
        return _insert(conn, "todos", dict(owner_id=_owner(owner_id), title=_text(title, "待办内容", max_length=5000), due_at=_timestamp(due_at), note_id=note_id, course_id=course_id, source_key=source_key, completed=0, created_at=_now()))


def update_todo(db_path, owner_id, todo_id, **fields):
    _check_fields(fields, ("title", "due_at", "completed", "course_id", "note_id"))
    if "title" in fields:
        fields["title"] = _text(fields["title"], "待办内容", max_length=5000)
    if "due_at" in fields:
        fields["due_at"] = _timestamp(fields["due_at"])
    if "completed" in fields:
        fields["completed"] = _boolean(fields["completed"])
    with _connection(db_path) as conn:
        _require_reference(conn, "courses", owner_id, fields.get("course_id"))
        _require_reference(conn, "notes", owner_id, fields.get("note_id"))
        return _update(conn, "todos", owner_id, todo_id, fields)


def delete_todo(db_path, owner_id, todo_id):
    return _delete(db_path, "todos", owner_id, todo_id)


def list_cards(db_path, owner_id, due_before=None):
    return _list_due(db_path, "cards", owner_id, due_before)


def get_card(db_path, owner_id, card_id):
    with _connection(db_path) as conn:
        return _get(conn, "cards", owner_id, card_id)


def _kind(value):
    if value not in ("qa", "cloze"):
        raise ValueError("复习卡类型无效")
    return value


def create_card(db_path, owner_id, front, back, due_at=None, note_id=None, course_id=None, kind="qa", source_key=None):
    with _connection(db_path) as conn:
        course_id = _validate_links(conn, owner_id, note_id, course_id)
        return _insert(conn, "cards", dict(owner_id=_owner(owner_id), front=_text(front, "卡片问题", max_length=20000), back=_text(back, "卡片答案", max_length=20000), due_at=_timestamp(due_at, nullable=False), note_id=note_id, course_id=course_id, kind=_kind(kind), source_key=source_key, step=0, created_at=_now()))


def update_card(db_path, owner_id, card_id, **fields):
    _check_fields(fields, ("front", "back", "due_at", "course_id", "note_id", "kind", "step"))
    for key in ("front", "back"):
        if key in fields:
            fields[key] = _text(fields[key], "卡片内容", max_length=20000)
    if "due_at" in fields:
        fields["due_at"] = _timestamp(fields["due_at"], nullable=False)
    if "kind" in fields:
        fields["kind"] = _kind(fields["kind"])
    if "step" in fields and (isinstance(fields["step"], bool) or not isinstance(fields["step"], int) or fields["step"] < 0):
        raise ValueError("复习阶段无效")
    with _connection(db_path) as conn:
        _require_reference(conn, "courses", owner_id, fields.get("course_id"))
        _require_reference(conn, "notes", owner_id, fields.get("note_id"))
        return _update(conn, "cards", owner_id, card_id, fields)


def delete_card(db_path, owner_id, card_id):
    return _delete(db_path, "cards", owner_id, card_id)


def review_card(db_path, owner_id, card_id, success, *, reviewed_at=None, intervals=(1, 3, 7, 15)):
    success = _boolean(success)
    if not intervals or any(isinstance(n, bool) or not isinstance(n, (int, float)) or n <= 0 for n in intervals):
        raise ValueError("复习间隔必须是正数")
    reviewed_at = _timestamp(reviewed_at, nullable=False)
    with _connection(db_path) as conn:
        card = _get(conn, "cards", owner_id, card_id)
        if card is None:
            return None
        interval = intervals[min(card["step"], len(intervals) - 1)] if success else intervals[0]
        next_step = min(card["step"] + 1, len(intervals)) if success else 0
        due_at = _timestamp(datetime.fromisoformat(reviewed_at) + timedelta(days=interval), nullable=False)
        updated = _update(conn, "cards", owner_id, card_id, dict(step=next_step, due_at=due_at))
        _insert(conn, "review_logs", dict(owner_id=_owner(owner_id), card_id=card_id, course_id=card["course_id"], success=success, reviewed_at=reviewed_at))
        return updated


def _sync_note_items(conn, owner_id, note, todos, cards):
    for table, items in (("todos", todos), ("cards", cards)):
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError("提取内容必须是列表")
        existing = {row["source_key"]: dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE owner_id=? AND note_id=? AND source_key IS NOT NULL", (owner_id, note["id"]))}
        occurrences = defaultdict(int)
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("提取内容格式无效")
            if table == "todos":
                content = {"title": _text(item.get("title"), "待办内容", max_length=5000)}
                identity = content["title"]
            else:
                content = dict(front=_text(item.get("front"), "卡片问题", max_length=20000), back=_text(item.get("back"), "卡片答案", max_length=20000), kind=_kind(item.get("kind", "cloze")))
                identity = content["kind"] + "\0" + content["front"] + "\0" + content["back"]
            occurrences[identity] += 1
            source_key = item.get("source_key") or hashlib.sha256((identity + "\0" + str(occurrences[identity])).encode()).hexdigest()
            source_key = _text(source_key, "提取内容标识", max_length=256)
            if source_key in seen:
                raise ValueError("提取内容标识重复")
            seen.add(source_key)
            if source_key in existing:
                _update(conn, table, owner_id, existing[source_key]["id"], {**content, "course_id": note["course_id"]})
            else:
                fields = {**content, "owner_id": _owner(owner_id), "note_id": note["id"], "course_id": note["course_id"], "source_key": source_key, "created_at": _now(), "due_at": _timestamp(item.get("due_at"), nullable=table == "todos")}
                if table == "todos":
                    fields["completed"] = _boolean(item.get("completed", False))
                else:
                    fields["step"] = 0
                _insert(conn, table, fields)
        for source_key in existing.keys() - seen:
            conn.execute(f"DELETE FROM {table} WHERE owner_id=? AND id=?", (owner_id, existing[source_key]["id"]))


def sync_note_items(db_path, owner_id, note_id, todos, cards):
    """Synchronize extracted items atomically, preserving matching review state.

    Manual items have a NULL source key and are never deleted by extraction.
    Passing None skips synchronization for that item type.
    """
    with _connection(db_path) as conn:
        note = _get(conn, "notes", owner_id, note_id)
        if note is None:
            return None
        _sync_note_items(conn, owner_id, note, todos, cards)
        return note
