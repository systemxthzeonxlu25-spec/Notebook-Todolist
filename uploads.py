"""Authenticated original-image attachments; metadata operations live in db.py."""
from io import BytesIO
from pathlib import Path
import os
import re
import uuid
import warnings

from flask import Blueprint, current_app, g, jsonify, request, send_file
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

import db

bp = Blueprint("uploads", __name__)
MAX_BYTES = 12 * 1024 * 1024
FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}


def stored_path(app, name):
    if not re.fullmatch(r"[0-9a-f-]{32,36}\.(?:jpg|jpeg|png|webp)", name):
        raise ValueError("附件存储名称无效")
    root = Path(app.config["UPLOAD_FOLDER"]).resolve()
    path = root / name
    if path.resolve().parent != root:
        raise ValueError("附件路径无效")
    return path


def public_attachment(row):
    return {key: row[key] for key in ("id", "note_id", "original_name", "media_type", "size", "created_at")} | {"url": f"/attachments/{row['id']}"}


def clean_files(app, attachments):
    for row in attachments:
        try:
            stored_path(app, row["stored_name"]).unlink(missing_ok=True)
        except OSError:
            app.logger.warning("附件清理未完成，请检查上传目录权限")


@bp.route("/api/notes/<int:note_id>/attachments", methods=["GET", "POST"])
def note_attachments(note_id):
    database, owner = current_app.config["DATABASE_PATH"], g.owner_id
    if not db.get_note(database, owner, note_id):
        return jsonify(error="笔记不存在"), 404
    if request.method == "GET":
        return jsonify(data=[public_attachment(row) for row in db.list_attachments(database, owner, note_id)])
    file = request.files.get("file")
    if not file or not file.filename:
        raise ValueError("请选择 JPEG、PNG 或 WEBP 图片")
    raw = file.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("原图不能为空，且不得超过 12 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format not in FORMATS or image.width * image.height > 24_000_000:
                    raise ValueError("仅支持 JPEG、PNG、WEBP，图片最多 2400 万像素")
                extension, media_type = FORMATS[image.format]
                image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("图片无法读取，请重新选择有效图片") from None
    name = uuid.uuid4().hex + extension
    path = stored_path(current_app, name)
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(raw)
        row = db.create_attachment(database, owner, note_id, name,
                                   secure_filename(file.filename)[:200] or f"board{extension}", media_type, len(raw))
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    return jsonify(data=public_attachment(row)), 201


@bp.get("/attachments/<int:attachment_id>")
def attachment_file(attachment_id):
    row = db.get_attachment(current_app.config["DATABASE_PATH"], g.owner_id, attachment_id)
    if not row:
        return jsonify(error="附件不存在"), 404
    path = stored_path(current_app, row["stored_name"])
    if not path.is_file():
        return jsonify(error="附件文件不存在，请从备份恢复或重新上传"), 404
    response = send_file(path, mimetype=row["media_type"], download_name=row["original_name"], max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.delete("/api/attachments/<int:attachment_id>")
def remove_attachment(attachment_id):
    database = current_app.config["DATABASE_PATH"]
    row = db.get_attachment(database, g.owner_id, attachment_id)
    if not row:
        return jsonify(error="附件不存在"), 404
    stored_path(current_app, row["stored_name"]).unlink(missing_ok=True)
    db.delete_attachment(database, g.owner_id, attachment_id)
    return jsonify(data=True)


def init_app(app):
    app.register_blueprint(bp)
