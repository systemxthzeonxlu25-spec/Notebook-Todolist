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
import uuid

from domain import sprint_due


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
    completed_at TEXT,
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
CREATE TABLE IF NOT EXISTS activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('note_created','card_reviewed','todo_completed')),
    source_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, kind, source_id)
);
CREATE INDEX IF NOT EXISTS idx_activity_owner_date ON activity_logs(owner_id, created_at);
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
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments(owner_id, note_id);
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


def positive_id(value, *, allow_string=False):
    """Validate IDs before binding them to SQLite's signed 64-bit INTEGER."""
    if allow_string and isinstance(value, str):
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            raise ValueError("记录编号必须是有效的正整数")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise ValueError("记录编号必须是有效的正整数")
    return value


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
        # Additive, idempotent upgrade: preserve every existing row and owner.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(todos)")}
        if "completed_at" not in columns:
            conn.execute("ALTER TABLE todos ADD COLUMN completed_at TEXT")
        # Only backfill timestamps that actually exist; old completed tasks have
        # no recorded completion date and must not fabricate today's activity.
        conn.execute("INSERT OR IGNORE INTO activity_logs(owner_id,kind,source_id,created_at) SELECT owner_id,'note_created',id,created_at FROM notes")
        conn.execute("INSERT OR IGNORE INTO activity_logs(owner_id,kind,source_id,created_at) SELECT owner_id,'card_reviewed',id,reviewed_at FROM review_logs")
        conn.execute("INSERT OR IGNORE INTO activity_logs(owner_id,kind,source_id,created_at) SELECT owner_id,'todo_completed',id,completed_at FROM todos WHERE completed_at IS NOT NULL")


def _get(conn, table, owner_id, item_id):
    item_id = positive_id(item_id)
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
    item_id = positive_id(item_id)
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
        params.append(positive_id(course_id))
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
        _record_activity(conn, owner_id, "note_created", note["id"], now)
        return note


def update_note(db_path, owner_id, note_id, *, todos=None, cards=None, now=None, **fields):
    _check_fields(fields, ("title", "content", "course_id"))
    if "title" in fields:
        fields["title"] = _text(fields["title"], "笔记标题", max_length=500)
    if "content" in fields:
        fields["content"] = _text(fields["content"], "笔记内容", empty=True)
    with _connection(db_path) as conn:
        existing = _get(conn, "notes", owner_id, note_id)
        if not existing:
            return None
        _require_reference(conn, "courses", owner_id, fields.get("course_id"))
        fields["updated_at"] = _timestamp(now, nullable=False)
        note = _update(conn, "notes", owner_id, note_id, fields)
        if "course_id" in fields:
            # Manual and extracted items follow their source note's course.
            for table in ("todos", "cards"):
                conn.execute(f"UPDATE {table} SET course_id=? WHERE owner_id=? AND note_id=?", (fields["course_id"], owner_id, note_id))
        _sync_note_items(conn, owner_id, note, todos, cards)
        if "course_id" in fields and fields["course_id"] != existing["course_id"]:
            _sync_sprint(conn, owner_id, fields["course_id"],
                         now or datetime.fromisoformat(fields["updated_at"]), note_id=note_id)
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
        existing = _get(conn, "todos", owner_id, todo_id)
        if existing is None:
            return None
        _require_reference(conn, "courses", owner_id, fields.get("course_id"))
        _require_reference(conn, "notes", owner_id, fields.get("note_id"))
        if "completed" in fields and bool(fields["completed"]) != existing["completed"]:
            fields["completed_at"] = _now() if fields["completed"] else None
            if fields["completed"]:
                _record_activity(conn, owner_id, "todo_completed", todo_id, fields["completed_at"])
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


def review_card(db_path, owner_id, card_id, success, *, reviewed_at=None, intervals=(1, 3, 7, 15), tz=None):
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
        course = _get(conn, "courses", owner_id, card["course_id"]) if card["course_id"] is not None else None
        if course and course["exam_date"]:
            local_now = datetime.fromisoformat(reviewed_at).astimezone(tz or timezone.utc)
            due_at = sprint_due(due_at, course["exam_date"], local_now)
        updated = _update(conn, "cards", owner_id, card_id, dict(step=next_step, due_at=due_at))
        log = _insert(conn, "review_logs", dict(owner_id=_owner(owner_id), card_id=card_id, course_id=card["course_id"], success=success, reviewed_at=reviewed_at))
        _record_activity(conn, owner_id, "card_reviewed", log["id"], reviewed_at)
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
                if table == "todos":
                    fields["completed_at"] = fields["created_at"] if fields["completed"] else None
                created = _insert(conn, table, fields)
                if table == "todos" and fields["completed"]:
                    _record_activity(conn, owner_id, "todo_completed", created["id"], fields["completed_at"])
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


# Authentication records are deliberately separate from content ownership.
# Existing local data is never claimed by registering or signing in.
def create_user(db_path, username, username_key, password_hash):
    """Create a user, returning safe account fields; duplicate names return None."""
    username = _text(username, "用户名", max_length=32)
    username_key = _text(username_key, "用户名标识", max_length=128)
    password_hash = _text(password_hash, "密码哈希", max_length=1000)
    user_id, created_at = str(uuid.uuid4()), _now()
    with _connection(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO users (id,username,username_key,password_hash,created_at) VALUES (?,?,?,?,?)",
                (user_id, username, username_key, password_hash, created_at),
            )
        except sqlite3.IntegrityError:
            return None
    return {"id": user_id, "username": username, "created_at": created_at}


def get_user(db_path, user_id):
    """Return safe fields only; never place password hashes in sessions or JSON."""
    with _connection(db_path) as conn:
        row = conn.execute("SELECT id,username,created_at FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row is not None else None


def get_auth_user(db_path, username_key):
    """Private authentication lookup. Callers must not serialize this result."""
    with _connection(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username_key=?", (username_key,)).fetchone()
        return dict(row) if row is not None else None


def reserve_auth_attempt(db_path, action, ip_key, identity_key, *, attempted_at=None,
                         pair_limit=5, ip_limit=30, window_seconds=900):
    """Atomically reserve an attempt, bounding failures and concurrent hashing.

    Failed login attempts remain. Successful logins release their pair's attempts
    with clear_login_attempts. Registration attempts remain regardless of result.
    Keys are one-way hashes, never passwords, API keys, or raw request bodies.
    """
    if action not in ("login", "register"):
        raise ValueError("认证操作无效")
    for limit in (pair_limit, ip_limit, window_seconds):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("认证限速配置无效")
    now = datetime.fromisoformat(_timestamp(attempted_at, nullable=False))
    cutoff = _timestamp(now - timedelta(seconds=window_seconds), nullable=False)
    retention = _timestamp(now - timedelta(seconds=max(window_seconds, 86400)), nullable=False)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM auth_attempts WHERE attempted_at<=?", (retention,))
        rows = list(conn.execute(
            "SELECT id,identity_key,attempted_at FROM auth_attempts WHERE action=? AND ip_key=? AND attempted_at>? ORDER BY attempted_at,id",
            (action, ip_key, cutoff),
        ))
        pair = [row for row in rows if row["identity_key"] == identity_key]
        blocking = []
        if len(pair) >= pair_limit:
            blocking.append(pair[-pair_limit])
        if len(rows) >= ip_limit:
            blocking.append(rows[-ip_limit])
        if blocking:
            expires = max(datetime.fromisoformat(row["attempted_at"]) + timedelta(seconds=window_seconds) for row in blocking)
            retry_after = max(1, int((expires - now).total_seconds()) + 1)
            return {"id": None, "retry_after": retry_after}
        cursor = conn.execute(
            "INSERT INTO auth_attempts(action,ip_key,identity_key,attempted_at) VALUES (?,?,?,?)",
            (action, ip_key, identity_key, _timestamp(now, nullable=False)),
        )
        return {"id": cursor.lastrowid, "retry_after": 0}


def clear_login_attempts(db_path, ip_key, identity_key):
    with _connection(db_path) as conn:
        conn.execute("DELETE FROM auth_attempts WHERE action='login' AND ip_key=? AND identity_key=?", (ip_key, identity_key))


def list_attachments(db_path, owner_id, note_id):
    note_id = positive_id(note_id)
    with _connection(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT a.* FROM attachments a JOIN notes n ON n.id=a.note_id AND n.owner_id=a.owner_id WHERE a.owner_id=? AND a.note_id=? ORDER BY a.id",
            (_owner(owner_id), note_id),
        )]


def get_attachment(db_path, owner_id, attachment_id):
    with _connection(db_path) as conn:
        return _get(conn, "attachments", owner_id, attachment_id)


def _attachment_name(value):
    if not isinstance(value, str) or "/" in value or "\\" in value or len(value) > 80:
        raise ValueError("附件存储名称无效")
    stem, dot, extension = value.rpartition(".")
    if not dot or extension not in {"jpg", "jpeg", "png", "webp"}:
        raise ValueError("附件存储名称无效")
    try:
        parsed = uuid.UUID(stem)
    except (ValueError, AttributeError) as exc:
        raise ValueError("附件存储名称必须使用 UUID") from exc
    if stem not in (parsed.hex, str(parsed)):
        raise ValueError("附件存储名称必须使用 UUID")
    return value


def create_attachment(db_path, owner_id, note_id, stored_name, original_name, media_type, size):
    stored_name = _attachment_name(stored_name)
    original_name = _text(original_name, "附件原始名称", max_length=255)
    if "\0" in original_name:
        raise ValueError("附件原始名称无效")
    if media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("附件必须是 JPEG、PNG 或 WEBP 图片")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 12 * 1024 * 1024:
        raise ValueError("图片大小必须介于 1 字节和 12 MiB 之间")
    if note_id is None:
        raise ValueError("附件必须关联笔记")
    with _connection(db_path) as conn:
        _require_reference(conn, "notes", owner_id, note_id)
        return _insert(conn, "attachments", dict(
            owner_id=_owner(owner_id), note_id=note_id, stored_name=stored_name,
            original_name=original_name, media_type=media_type, size=size, created_at=_now(),
        ))


def delete_attachment(db_path, owner_id, attachment_id):
    return _delete(db_path, "attachments", owner_id, attachment_id)


def _manual_items(items):
    if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, dict) for item in items):
        raise ValueError("确认内容必须是最多 100 项的列表")
    return items


def create_manual_todos(db_path, owner_id, items, note_id=None, course_id=None):
    """Persist a confirmed set atomically; extraction never removes these items."""
    items = _manual_items(items)
    with _connection(db_path) as conn:
        course_id = _validate_links(conn, owner_id, note_id, course_id)
        result = []
        for item in items:
            result.append(_insert(conn, "todos", dict(
                owner_id=_owner(owner_id), note_id=note_id, course_id=course_id,
                source_key=None, title=_text(item.get("title"), "待办内容", max_length=5000),
                due_at=_timestamp(item.get("due_at")), completed=0, created_at=_now(),
            )))
        return result


def create_manual_cards(db_path, owner_id, items, note_id=None, course_id=None):
    """Persist confirmed AI questions without allowing client-supplied source IDs."""
    items = _manual_items(items)
    with _connection(db_path) as conn:
        course_id = _validate_links(conn, owner_id, note_id, course_id)
        result = []
        for item in items:
            result.append(_insert(conn, "cards", dict(
                owner_id=_owner(owner_id), note_id=note_id, course_id=course_id,
                source_key=None, front=_text(item.get("front"), "卡片问题", max_length=20000),
                back=_text(item.get("back"), "卡片答案", max_length=20000),
                kind="qa", due_at=_timestamp(item.get("due_at"), nullable=False), step=0, created_at=_now(),
            )))
        return result


def _record_activity(conn, owner_id, kind, source_id, created_at):
    """Keep history after deleting content, and count a task's completion once."""
    conn.execute(
        "INSERT OR IGNORE INTO activity_logs(owner_id,kind,source_id,created_at) VALUES (?,?,?,?)",
        (_owner(owner_id), kind, source_id, _timestamp(created_at, nullable=False)),
    )


def _sync_sprint(conn, owner_id, course_id, now, *, note_id=None):
    """Advance scoped cards inside their parent's write transaction."""
    course = _get(conn, "courses", owner_id, course_id) if course_id is not None else None
    if not course or not course["exam_date"]:
        return
    conditions, params = ["owner_id=?", "course_id=?"], [_owner(owner_id), course_id]
    if note_id is not None:
        conditions.append("note_id=?")
        params.append(note_id)
    rows = conn.execute("SELECT id,due_at FROM cards WHERE " + " AND ".join(conditions), params).fetchall()
    for row in rows:
        due_at = sprint_due(row["due_at"], course["exam_date"], now, first_round=True)
        conn.execute("UPDATE cards SET due_at=? WHERE owner_id=? AND id=?", (due_at, owner_id, row["id"]))


def set_course_exam(db_path, owner_id, course_id, exam_date, now):
    """Save the exam and pull all existing cards forward in one transaction."""
    exam_date = _exam_date(exam_date)
    with _connection(db_path) as conn:
        course = _update(conn, "courses", owner_id, course_id, {"exam_date": exam_date})
        if course is None:
            return None
        _sync_sprint(conn, owner_id, course_id, now)
        return course


def learning_statistics(db_path, owner_id, *, since=None, until=None):
    """Return all-time course rates plus a bounded range of activity events.

    Accuracy is successes / recorded answers, not percent of cards mastered.
    Todo rate is the currently completed fraction of currently existing todos.
    Historical review statistics follow review_logs' existing cascade behavior;
    the heatmap log itself persists after deleting source content.
    """
    owner_id = _owner(owner_id)
    with _connection(db_path) as conn:
        reviews = {row["course_id"]: dict(row) for row in conn.execute(
            "SELECT course_id,COUNT(*) AS attempts,SUM(success) AS correct FROM review_logs WHERE owner_id=? GROUP BY course_id",
            (owner_id,),
        )}
        todos = {row["course_id"]: dict(row) for row in conn.execute(
            "SELECT course_id,COUNT(*) AS total,SUM(completed) AS completed FROM todos WHERE owner_id=? GROUP BY course_id",
            (owner_id,),
        )}
        courses = [dict(row) for row in conn.execute("SELECT id,name FROM courses WHERE owner_id=? ORDER BY id", (owner_id,))]
        if None in reviews or None in todos:
            courses.append({"id": None, "name": "未归类"})
        result = []
        for course in courses:
            review = reviews.get(course["id"], {})
            todo = todos.get(course["id"], {})
            attempts, correct = review.get("attempts", 0), review.get("correct", 0)
            total, completed = todo.get("total", 0), todo.get("completed", 0)
            result.append({"course_id": course["id"], "course_name": course["name"],
                           "review_attempts": attempts, "review_correct": correct,
                           "review_accuracy": round(correct / attempts, 4) if attempts else None,
                           "todo_total": total, "todo_completed": completed,
                           "todo_completion_rate": round(completed / total, 4) if total else None})
        params = [owner_id]
        conditions = ["owner_id=?"]
        if since is not None:
            conditions.append("created_at>=?")
            params.append(_timestamp(since, nullable=False))
        if until is not None:
            conditions.append("created_at<?")
            params.append(_timestamp(until, nullable=False))
        events = [dict(row) for row in conn.execute(
            "SELECT kind,created_at FROM activity_logs WHERE " + " AND ".join(conditions) + " ORDER BY created_at,id", params,
        )]
        total = sum(item["todo_total"] for item in result)
        completed = sum(item["todo_completed"] for item in result)
        return {"courses": result, "events": events,
                "todo_total": total, "todo_completed": completed,
                "todo_completion_rate": round(completed / total, 4) if total else None}
