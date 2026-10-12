"""时间和笔记派生内容的纯业务规则；数据库访问一律交给 db.py。"""
from datetime import datetime, timedelta, timezone
import hashlib
import re


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_due(value, tz):
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return utc_iso(parsed)
    except (TypeError, ValueError):
        raise ValueError("截止时间格式不正确") from None


def current_course(schedule, now):
    clock = now.strftime("%H:%M")
    return next((row for row in schedule if row["weekday"] == now.weekday()
                 and row["start_time"] <= clock < row["end_time"]), None)


def next_lesson(schedule, course_id, now):
    candidates = []
    for row in schedule:
        if row["course_id"] != course_id:
            continue
        hour, minute = map(int, row["start_time"].split(":"))
        candidate = (now + timedelta(days=(row["weekday"] - now.weekday()) % 7)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)
        candidates.append(candidate)
    return utc_iso(min(candidates)) if candidates else None


def extract_items(content, schedule, course_id, now):
    """只提取非代码块内容；稳定内容标识保留完成/复习状态。"""
    todos, cards, counts = [], [], {}
    in_fence = False
    due = next_lesson(schedule, course_id, now)
    for line in content.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        task = re.match(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$", line)
        if task:
            title = task.group(2)
            key = "todo:" + title
            counts[key] = counts.get(key, 0) + 1
            source_key = hashlib.sha256(f"{key}:{counts[key]}".encode()).hexdigest()
            todos.append({"title": title, "source_key": source_key, "due_at": due,
                          "completed": task.group(1).lower() == "x", "course_id": course_id})
        matches = list(re.finditer(r"==(.+?)==", line))
        for match in matches:
            back = match.group(1).strip()
            if not back:
                continue
            front = (line[:match.start()] + "［填空］" + line[match.end():]).replace("==", "")
            key = "card:" + front + ":" + back
            counts[key] = counts.get(key, 0) + 1
            source_key = hashlib.sha256(f"{key}:{counts[key]}".encode()).hexdigest()
            cards.append({"front": front, "back": back, "kind": "cloze", "source_key": source_key,
                          "course_id": course_id, "due_at": utc_iso(now)})
    return todos, cards
