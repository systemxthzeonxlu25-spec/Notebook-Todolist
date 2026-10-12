"""时间和笔记派生内容的纯业务规则；数据库访问一律交给 db.py。"""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
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
    fence_char, fence_length = None, 0
    due = next_lesson(schedule, course_id, now)
    for line in content.splitlines():
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_char:
            if fence and fence.group(1)[0] == fence_char and len(fence.group(1)) >= fence_length and not fence.group(2).strip():
                fence_char, fence_length = None, 0
            continue
        if fence:
            fence_char, fence_length = fence.group(1)[0], len(fence.group(1))
            continue
        if line.startswith(("    ", "\t")):
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


def sprint_due(due_at, exam_date, now, *, first_round=False):
    """Compress a due date while retaining normal scheduling after exam start.

    Exam dates mean 00:00 in the application's timezone. On activation the first
    due date is no later than one third of the remaining time. Following each
    review it is no later than half of the remaining time. Two timely reviews
    can therefore occur before the exam. This schedules opportunities; it cannot
    guarantee user participation or useful spacing when very little time remains.
    Existing overdue cards remain overdue. All returned values are UTC ISO dates.
    """
    due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    if due.tzinfo is None or now.tzinfo is None:
        raise ValueError("复习日期必须包含时区")
    if exam_date:
        exam_start = datetime.combine(date.fromisoformat(exam_date), time.min, now.tzinfo)
        # UTC arithmetic handles DST transitions for configurable app timezones.
        remaining = exam_start.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        if remaining > timedelta(0):
            divisor = 3 if first_round else 2
            cap = now.astimezone(timezone.utc) + remaining / divisor
            due = min(due, cap)
    return due.astimezone(timezone.utc).isoformat(timespec="seconds")


def exam_countdowns(courses, now):
    """Today is included as day 0; past exams disappear from the today page."""
    result = []
    for course in courses:
        if not course.get("exam_date"):
            continue
        days = (date.fromisoformat(course["exam_date"]) - now.date()).days
        if days >= 0:
            result.append({"course_id": course["id"], "course_name": course["name"],
                           "exam_date": course["exam_date"], "days_left": days})
    return sorted(result, key=lambda item: (item["exam_date"], item["course_id"]))


def learning_heatmap(events, now, *, days=84):
    """One event = note creation, review answer, or a task's first completion.

    Dates use the app timezone, including DST. Days with no activity are kept so
    the frontend only needs to render the provided sequence, not calculate dates.
    """
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 366:
        raise ValueError("热力图天数必须为 1 到 366")
    first_day = now.date() - timedelta(days=days - 1)
    counts = Counter()
    for event in events:
        local_day = datetime.fromisoformat(event["created_at"]).astimezone(now.tzinfo).date()
        if first_day <= local_day <= now.date():
            counts[local_day] += 1
    return [{"date": (first_day + timedelta(days=offset)).isoformat(),
             "count": counts[first_day + timedelta(days=offset)]}
            for offset in range(days)]
