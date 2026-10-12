"""学生笔记 Web 应用。运行：python app.py。"""
from datetime import datetime, time, timedelta
from pathlib import Path
import os
import secrets
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request, session, send_from_directory
from werkzeug.exceptions import HTTPException

import db
import auth
import uploads
import ai
from domain import current_course, extract_items, next_lesson, parse_due, utc_iso

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def create_app(test_config=None):
    app = Flask(__name__)
    production = os.getenv("APP_ENV") == "production"
    secret = os.getenv("SECRET_KEY")
    if production and not secret and not test_config:
        raise RuntimeError("生产环境必须通过环境变量设置 SECRET_KEY")
    app.config.update(
        SECRET_KEY=secret or secrets.token_hex(32),
        DATABASE_PATH=str(ROOT / os.getenv("DATABASE_PATH", "instance/notebook.sqlite3")),
        UPLOAD_FOLDER=str(ROOT / os.getenv("UPLOAD_FOLDER", "instance/uploads")),
        TZ=os.getenv("TZ", "Asia/Shanghai"),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", "16777216")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        TEMPLATES_AUTO_RELOAD=not production,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE_PATH"])
    auth.init_app(app)
    uploads.init_app(app)
    ai.init_app(app)

    def now():
        provider = app.config.get("NOW_PROVIDER")
        return provider() if provider else datetime.now(ZoneInfo(app.config["TZ"]))

    def database():
        return app.config["DATABASE_PATH"], g.owner_id

    def data(value, status=200):
        return jsonify(data=value), status

    def payload():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求必须是 JSON 对象")
        return body

    def text_field(body, key, default="", limit=200):
        value = body.get(key, default)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{key} 内容过长或格式不正确")
        return value.strip() if key != "content" else value

    def course_value(value):
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError("课程格式不正确")
        try:
            course_id = int(value)
        except (TypeError, ValueError):
            raise ValueError("课程格式不正确") from None
        if not db.get_course(*database(), course_id):
            raise ValueError("课程不存在")
        return course_id

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    @app.before_request
    def prepare_request():
        result = auth.authenticate_request()
        if result is not None:
            return result
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf_token", "")
            if not expected or not secrets.compare_digest(supplied, expected):
                return jsonify(error="页面安全凭证已失效，请刷新后重试"), 403

    @app.context_processor
    def template_context():
        return {"csrf_token": csrf_token(), "app_timezone": app.config["TZ"],
                "current_user": auth.public_user(g.user)}

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self' https://cdn.jsdelivr.net; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif response.mimetype == "text/html" and request.path != "/offline":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def bad_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {404: "内容不存在", 413: "提交内容过大，请压缩后重试", 429: "操作过于频繁，请稍后再试"}
        return jsonify(error=messages.get(error.code, "请求无法处理")), error.code

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/api/session")
    def session_info():
        return data({"csrf_token": csrf_token(), "timezone": app.config["TZ"],
                     "user": auth.public_user(g.user),
                     "registration_enabled": app.config["REGISTER_ENABLED"]})

    @app.get("/")
    def index():
        return render_template("today.html")

    @app.get("/notes")
    def notes_page():
        return render_template("notes.html")

    @app.get("/notes/new")
    def new_note_page():
        return render_template("editor.html", note_id=None)

    @app.get("/quick")
    def quick_note_page():
        return render_template("editor.html", note_id=None, quick=True)

    @app.get("/sw.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "sw.js", max_age=0)
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.webmanifest")

    @app.get("/offline")
    def offline():
        return send_from_directory(app.static_folder, "offline.html")

    @app.get("/notes/<int:note_id>")
    def editor_page(note_id):
        if not db.get_note(*database(), note_id):
            return jsonify(error="笔记不存在"), 404
        return render_template("editor.html", note_id=note_id)

    @app.get("/schedule")
    def schedule_page():
        return render_template("schedule.html")

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html")

    @app.route("/api/courses", methods=["GET", "POST"])
    def courses():
        if request.method == "GET":
            return data(db.list_courses(*database()))
        name = text_field(payload(), "name", limit=80)
        if not name:
            raise ValueError("请输入课程名称")
        return data(db.create_course(*database(), name), 201)

    @app.route("/api/courses/<int:course_id>", methods=["PATCH", "DELETE"])
    def change_course(course_id):
        if request.method == "DELETE":
            result = db.delete_course(*database(), course_id)
        else:
            name = text_field(payload(), "name", limit=80)
            if not name:
                raise ValueError("请输入课程名称")
            result = db.update_course(*database(), course_id, name=name)
        return data(result) if result else (jsonify(error="课程不存在"), 404)

    @app.route("/api/schedule", methods=["GET", "POST"])
    def schedule():
        if request.method == "GET":
            return data(db.list_schedule(*database()))
        body = payload()
        course_id = course_value(body.get("course_id"))
        if course_id is None:
            raise ValueError("请选择课程")
        weekday = body.get("weekday")
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError("星期必须为 0 到 6")
        start, end = body.get("start_time", ""), body.get("end_time", "")
        try:
            if not isinstance(start, str) or not isinstance(end, str) or len(start) != 5 or len(end) != 5:
                raise ValueError
            time.fromisoformat(start)
            time.fromisoformat(end)
            if start >= end:
                raise ValueError
        except ValueError:
            raise ValueError("课程起止时间不正确，请按 HH:MM 填写且结束晚于开始") from None
        for slot in db.list_schedule(*database()):
            if slot["weekday"] == weekday and start < slot["end_time"] and end > slot["start_time"]:
                raise ValueError("这个时间已有课程，请先调整冲突时段")
        return data(db.create_schedule(*database(), course_id, weekday, start, end), 201)

    @app.delete("/api/schedule/<int:slot_id>")
    def remove_slot(slot_id):
        return data(True) if db.delete_schedule(*database(), slot_id) else (jsonify(error="课表时段不存在"), 404)

    @app.get("/api/current-course")
    def get_current_course():
        slot = current_course(db.list_schedule(*database()), now())
        return data({"course_id": slot["course_id"] if slot else None,
                     "course_name": slot.get("course_name") if slot else None})

    def save_note(note_id=None):
        body = payload()
        existing = db.get_note(*database(), note_id) if note_id else None
        if note_id and not existing:
            return jsonify(error="笔记不存在"), 404
        schedule_rows = db.list_schedule(*database())
        active = current_course(schedule_rows, now())
        default_course = existing["course_id"] if existing else (active["course_id"] if active else None)
        course_id = course_value(body.get("course_id", default_course))
        title = text_field(body, "title", (existing or {}).get("title", "未命名笔记")) or "未命名笔记"
        content = text_field(body, "content", (existing or {}).get("content", ""), limit=1_000_000)
        todos, cards = extract_items(content, schedule_rows, course_id, now())
        if note_id:
            return data(db.update_note(*database(), note_id, title=title, content=content,
                                       course_id=course_id, todos=todos, cards=cards))
        return data(db.create_note(*database(), title, content, course_id=course_id,
                                   todos=todos, cards=cards), 201)

    @app.route("/api/notes", methods=["GET", "POST"])
    def notes():
        if request.method == "POST":
            return save_note()
        course_id = course_value(request.args.get("course_id"))
        return data(db.list_notes(*database(), query=request.args.get("q", "")[:200],
                                  course_id=course_id, unclassified=request.args.get("unclassified") == "1"))

    @app.route("/api/notes/<int:note_id>", methods=["GET", "PUT", "DELETE"])
    def note(note_id):
        if request.method == "PUT":
            return save_note(note_id)
        if request.method == "DELETE":
            attachments = db.list_attachments(*database(), note_id)
            result = db.delete_note(*database(), note_id)
            if result:
                uploads.clean_files(app, attachments)
        else:
            result = db.get_note(*database(), note_id)
        return data(result) if result else (jsonify(error="笔记不存在"), 404)

    def manual_context(body):
        note_id = body.get("note_id")
        if note_id is not None:
            if isinstance(note_id, bool) or not isinstance(note_id, int):
                raise ValueError("笔记格式不正确")
            note = db.get_note(*database(), note_id)
            if not note:
                raise ValueError("笔记不存在")
        else:
            note = None
        course_id = course_value(body.get("course_id", note["course_id"] if note else None))
        items = body.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 100 or not all(isinstance(item, dict) for item in items):
            raise ValueError("请提交 1 到 100 条确认后的内容")
        return note_id, course_id, items

    @app.route("/api/todos", methods=["GET", "POST"])
    def todos():
        if request.method == "POST":
            note_id, course_id, items = manual_context(payload())
            default_due = next_lesson(db.list_schedule(*database()), course_id, now())
            parsed = [{"title": text_field(item, "title", limit=5000),
                       "due_at": parse_due(item.get("due_at"), ZoneInfo(app.config["TZ"])) or default_due} for item in items]
            return data(db.create_manual_todos(*database(), parsed, note_id=note_id, course_id=course_id), 201)
        return data(db.list_todos(*database()))

    @app.patch("/api/todos/<int:todo_id>")
    def edit_todo(todo_id):
        body, changes = payload(), {}
        if "completed" in body:
            if not isinstance(body["completed"], bool):
                raise ValueError("完成状态必须是布尔值")
            changes["completed"] = body["completed"]
        if "due_at" in body:
            changes["due_at"] = parse_due(body["due_at"], ZoneInfo(app.config["TZ"]))
        result = db.update_todo(*database(), todo_id, **changes)
        return data(result) if result else (jsonify(error="待办不存在"), 404)

    @app.route("/api/cards", methods=["GET", "POST"])
    def cards():
        if request.method == "POST":
            note_id, course_id, items = manual_context(payload())
            parsed = [{"front": text_field(item, "front", limit=20000),
                       "back": text_field(item, "back", limit=20000), "due_at": utc_iso(now())} for item in items]
            return data(db.create_manual_cards(*database(), parsed, note_id=note_id, course_id=course_id), 201)
        return data(db.list_cards(*database()))

    @app.post("/api/cards/<int:card_id>/review")
    def review(card_id):
        remembered = payload().get("remembered")
        if not isinstance(remembered, bool):
            raise ValueError("请选择记住了或没记住")
        result = db.review_card(*database(), card_id, remembered, reviewed_at=utc_iso(now()))
        return data(result) if result else (jsonify(error="复习卡不存在"), 404)

    @app.get("/api/today")
    def today():
        current = now()
        cutoff = utc_iso(datetime.combine(current.date(), time.max, current.tzinfo))
        return data({"date": current.date().isoformat(), "tz": app.config["TZ"],
                     "todos": [item for item in db.list_todos(*database(), due_before=cutoff) if not item["completed"]],
                     "cards": db.list_cards(*database(), due_before=cutoff), "exams": []})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)
