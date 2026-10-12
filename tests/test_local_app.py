"""Local launcher checks, including real Waitress and process restart tests."""
from contextlib import contextmanager
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_app.py"
_spec = importlib.util.spec_from_file_location("notebook_local_launcher", SCRIPT)
local_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(local_app)


def available_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request_json(opener, base, path, body=None, csrf=None):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with opener.open(request, timeout=3) as response:
        return json.loads(response.read()), response.headers


@contextmanager
def live_process(data_directory, port):
    environment = {**os.environ, "NOTEBOOK_DATA_DIR": str(data_directory), "PYTHONIOENCODING": "utf-8",
                   "HOST": "0.0.0.0", "PORT": "1", "SESSION_COOKIE_SECURE": "true", "TRUST_PROXY": "true",
                   "REGISTER_ENABLED": "false", "SECRET_KEY": "ambient-config-must-not-be-used",
                   "DATABASE_PATH": str(data_directory / "wrong.sqlite3"), "UPLOAD_FOLDER": str(data_directory / "wrong-uploads")}
    process = subprocess.Popen([sys.executable, str(SCRIPT), "--no-browser", "--port", str(port)],
                               cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    opener = build_opener(ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output, errors = process.communicate()
                pytest.fail(f"Local launcher exited before readiness: {output} {errors}")
            try:
                result, _ = request_json(opener, base, "/healthz")
                if result == {"status": "ok"}:
                    break
            except (OSError, URLError):
                time.sleep(0.05)
        else:
            pytest.fail("Local launcher did not become ready")
        yield process, base
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        output, errors = process.communicate()
        secret_file = data_directory / "secret_key"
        if secret_file.is_file():
            secret = secret_file.read_text().strip()
            assert secret not in output and secret not in errors


def test_platform_data_paths_and_explicit_override(tmp_path):
    assert local_app.data_directory({}, platform="win32", home=tmp_path) == tmp_path / "AppData" / "Local" / "Notebook-Todolist"
    assert local_app.data_directory({"LOCALAPPDATA": str(tmp_path / "Local")}, platform="win32") == tmp_path / "Local" / "Notebook-Todolist"
    assert local_app.data_directory({}, platform="linux", home=tmp_path) == tmp_path / ".local" / "share" / "Notebook-Todolist"
    assert local_app.data_directory({"XDG_DATA_HOME": str(tmp_path / "xdg")}, platform="linux") == tmp_path / "xdg" / "Notebook-Todolist"
    for platform in ("win32", "linux"):
        assert local_app.data_directory({"NOTEBOOK_DATA_DIR": str(tmp_path / "chosen")}, platform=platform) == tmp_path / "chosen"


def test_secret_is_private_persistent_and_never_overwrites_existing_content(tmp_path):
    directory = tmp_path / "private-data"
    first = local_app.persistent_secret(directory)
    path = directory / "secret_key"
    original, modified_at = path.read_bytes(), path.stat().st_mtime_ns
    assert len(first) >= 32
    assert local_app.persistent_secret(directory) == first
    assert path.read_bytes() == original and path.stat().st_mtime_ns == modified_at
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("invalid-existing-key")
    with pytest.raises(ValueError):
        local_app.persistent_secret(directory)
    assert path.read_text() == "invalid-existing-key"


def test_local_environment_overrides_unsafe_ambient_settings_without_editing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOK_DATA_DIR", str(tmp_path / "data"))
    names = ("APP_ENV", "SECRET_KEY", "DATABASE_PATH", "UPLOAD_FOLDER", "HOST", "PORT", "SESSION_COOKIE_SECURE",
             "TRUST_PROXY", "REGISTER_ENABLED", "TZ")
    for name in names:
        monkeypatch.setenv(name, "unsafe-ambient-value")
    config = tmp_path / "existing.env"
    config.write_text("Do not alter this configuration\n")
    directory = local_app.configure_environment(19876)
    assert os.environ["HOST"] == "127.0.0.1"
    assert os.environ["PORT"] == "19876"
    assert os.environ["APP_ENV"] == "production"
    assert os.environ["SECRET_KEY"] == (directory / "secret_key").read_text().strip()
    assert os.environ["DATABASE_PATH"] == str(directory / "notebook.sqlite3")
    assert os.environ["UPLOAD_FOLDER"] == str(directory / "uploads")
    assert os.environ["SESSION_COOKIE_SECURE"] == os.environ["TRUST_PROXY"] == "false"
    assert os.environ["REGISTER_ENABLED"] == "true"
    assert os.environ["TZ"] == "Asia/Shanghai"
    assert config.read_text() == "Do not alter this configuration\n"


def test_waitress_binds_only_loopback_with_two_threads():
    pytest.importorskip("waitress")

    def application(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"local"]

    server = local_app.create_local_server(application, 0)
    try:
        assert server.socket.getsockname()[0] == "127.0.0.1"
        assert server.adj.threads == 2
    finally:
        server.close()
        server.task_dispatcher.shutdown()


def test_real_local_http_session_and_data_survive_process_restart(tmp_path):
    pytest.importorskip("waitress")
    directory = tmp_path / "persistent-data"
    port = available_port()
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    with live_process(directory, port) as (_, base):
        response, _ = request_json(opener, base, "/api/session")
        token = response["data"]["csrf_token"]
        response, headers = request_json(opener, base, "/api/auth/register",
                                         {"username": "local-user", "password": "local-test-password"}, token)
        token = response["data"]["csrf_token"]
        assert "HttpOnly" in headers["Set-Cookie"]
        assert "; Secure" not in headers["Set-Cookie"]
        response, _ = request_json(opener, base, "/api/notes", {"title": "重启后保留", "content": "- [ ] 本地任务\n==知识点=="}, token)
        note_id = response["data"]["id"]
        original_secret = (directory / "secret_key").read_bytes()
        assert (directory / "notebook.sqlite3").is_file()
        assert (directory / "uploads").is_dir()
        assert not (directory / "wrong.sqlite3").exists()
        assert not (directory / "wrong-uploads").exists()
    with live_process(directory, port) as (_, base):
        response, _ = request_json(opener, base, "/api/notes")
        assert [note["id"] for note in response["data"]] == [note_id]
        assert response["data"][0]["title"] == "重启后保留"
        assert (directory / "secret_key").read_bytes() == original_secret
        response, _ = request_json(opener, base, "/api/todos")
        assert response["data"][0]["title"] == "本地任务"


def test_occupied_port_is_reported_without_interrupting_existing_service(tmp_path):
    pytest.importorskip("waitress")

    class ExistingService(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"existing-service-intact")

        def log_message(self, *args):
            pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), ExistingService)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        port = service.server_port
        result = subprocess.run([sys.executable, str(SCRIPT), "--no-browser", "--port", str(port)],
                                cwd=ROOT, env={**os.environ, "NOTEBOOK_DATA_DIR": str(tmp_path / "conflict-data"), "PYTHONIOENCODING": "utf-8"},
                                capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        assert result.returncode == 1
        assert str(port) in result.stderr and "已被占用" in result.stderr
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/", timeout=2) as response:
            assert response.read() == b"existing-service-intact"
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)


def test_browser_is_opened_only_after_successful_health_check(monkeypatch):
    events = []

    class HealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            events.append("healthy")
            return b'{"status":"ok"}'

    class Opener:
        def open(self, url, timeout):
            return HealthResponse()

    monkeypatch.setattr(local_app, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(local_app.webbrowser, "open", lambda url, new: events.append("browser") or True)
    local_app.announce_when_ready("http://127.0.0.1:18765/", True, threading.Event())
    assert events == ["healthy", "browser"]
    events.clear()
    local_app.announce_when_ready("http://127.0.0.1:18765/", False, threading.Event())
    assert events == ["healthy"]


def test_real_attachment_session_and_backup_restore_survive_restart(tmp_path):
    """A complete local backup restores the account, note, original image and session."""
    from contextlib import closing
    from io import BytesIO
    import shutil
    import sqlite3
    from PIL import Image

    pytest.importorskip("waitress")
    directory = tmp_path / "original-data"
    restored = tmp_path / "restored-data"
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    image = BytesIO()
    Image.new("RGB", (12, 8), (40, 100, 180)).save(image, format="PNG")
    original = image.getvalue()
    with live_process(directory, available_port()) as (_, base):
        result, _ = request_json(opener, base, "/api/session")
        result, _ = request_json(opener, base, "/api/auth/register",
                                 {"username": "backup-local-user", "password": "local-backup-test-password"},
                                 result["data"]["csrf_token"])
        token = result["data"]["csrf_token"]
        account = result["data"]["user"]
        result, _ = request_json(opener, base, "/api/notes",
                                 {"title": "Windows 备份恢复", "content": "==原图与会话也要保留=="}, token)
        note_id = result["data"]["id"]
        boundary = "notebook-real-local-image-boundary"
        multipart = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="original.png"\r\n'
            'Content-Type: image/png\r\n\r\n'
        ).encode() + original + f"\r\n--{boundary}--\r\n".encode()
        request = Request(base + f"/api/notes/{note_id}/attachments", data=multipart,
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                   "X-CSRF-Token": token})
        with opener.open(request, timeout=3) as response:
            attachment = json.loads(response.read())["data"]
        assert attachment["size"] == len(original)

    # The local app is stopped before copying attachments and its session key.
    # A new directory is used; restoring never replaces the original data.
    restored.mkdir()
    with closing(sqlite3.connect((directory / "notebook.sqlite3").as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(restored / "notebook.sqlite3")) as target:
            source.backup(target)
            assert target.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    shutil.copy2(directory / "secret_key", restored / "secret_key")
    shutil.copytree(directory / "uploads", restored / "uploads")

    for data in (directory, restored):
        with live_process(data, available_port()) as (_, base):
            result, _ = request_json(opener, base, "/api/session")
            assert result["data"]["user"] == account
            result, _ = request_json(opener, base, f"/api/notes/{note_id}")
            assert result["data"]["title"] == "Windows 备份恢复"
            result, _ = request_json(opener, base, f"/api/notes/{note_id}/attachments")
            assert result["data"] == [attachment]
            with opener.open(base + attachment["url"], timeout=3) as response:
                assert response.read() == original
                assert response.headers["Cache-Control"] == "private, no-store"
    assert (directory / "secret_key").read_bytes() == (restored / "secret_key").read_bytes()
