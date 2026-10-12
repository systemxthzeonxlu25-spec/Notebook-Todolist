#!/usr/bin/env python3
"""Run the local notebook application on loopback with persistent private data.

Dependency installation belongs to bootstrap_local.py. This launcher never
changes a repository .env file or takes over an occupied port.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
DEFAULT_PORT = 18765


def data_directory(environ=None, platform=None, home=None):
    environment = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    override = environment.get("NOTEBOOK_DATA_DIR")
    if override:
        directory = Path(override).expanduser()
    elif platform.startswith("win"):
        directory = Path(environment.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Notebook-Todolist"
    else:
        directory = Path(environment.get("XDG_DATA_HOME") or home / ".local" / "share") / "Notebook-Todolist"
    return directory.resolve()


def persistent_secret(directory):
    """Create once using exclusive creation; keep existing key contents intact."""
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "secret_key"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("本机密钥文件必须是普通文件；现有文件未改动") from None
    else:
        with os.fdopen(descriptor, "w", encoding="ascii") as secret_file:
            secret_file.write(secrets.token_urlsafe(48) + "\n")
            secret_file.flush()
            os.fsync(secret_file.fileno())
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="ascii") as secret_file:
        secret = secret_file.read(1024).strip()
    if not 32 <= len(secret) <= 512 or not secret.isascii() or any(char.isspace() for char in secret):
        raise ValueError("本机密钥文件内容无效；现有文件未改动，请恢复其备份")
    return secret


def configure_environment(port):
    """Set local-only runtime values before importing app's module-level app."""
    directory = data_directory()
    secret = persistent_secret(directory)
    os.environ.update({
        "APP_ENV": "production",
        "SECRET_KEY": secret,
        "DATABASE_PATH": str(directory / "notebook.sqlite3"),
        "UPLOAD_FOLDER": str(directory / "uploads"),
        "HOST": HOST,
        "PORT": str(port),
        "SESSION_COOKIE_SECURE": "false",
        "TRUST_PROXY": "false",
        "REGISTER_ENABLED": "true",
        "TZ": "Asia/Shanghai",
    })
    return directory


def create_local_server(application, port):
    from waitress import create_server

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # On Windows SO_REUSEADDR can take over another service's address.
        # Bind exclusively before handing the socket to Waitress.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, port))
        return create_server(application, sockets=[listener], threads=2, ident="Notebook-Todolist")
    except Exception:
        listener.close()
        raise


def announce_when_ready(url, open_browser, stop_event):
    # Ignore system HTTP proxies for this strictly local readiness request.
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 20
    while not stop_event.is_set() and time.monotonic() < deadline:
        try:
            with opener.open(url + "healthz", timeout=0.5) as response:
                ready = response.status == 200 and json.loads(response.read(4096)).get("status") == "ok"
        except (OSError, URLError, ValueError):
            ready = False
        if ready:
            print(f"本机应用已就绪：{url}", flush=True)
            print("仅本机可以访问；按 Ctrl+C 或关闭此窗口停止。", flush=True)
            if open_browser:
                try:
                    opened = webbrowser.open(url, new=2)
                except (OSError, webbrowser.Error):
                    opened = False
                if not opened:
                    print(f"浏览器未自动打开，请手动访问：{url}", flush=True)
            return
        stop_event.wait(0.1)
    if not stop_event.is_set():
        print(f"应用仍未通过就绪检查，请查看当前窗口或访问：{url}", file=sys.stderr, flush=True)


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是 1 至 65535 的整数") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1 至 65535 的整数")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="在本机启动学生笔记，固定监听 127.0.0.1。")
    parser.add_argument("--port", type=port_number, default=DEFAULT_PORT, help=f"本机端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--no-browser", action="store_true", help="就绪后不自动打开浏览器")
    arguments = parser.parse_args(argv)
    # SQLite files and uploads created by the application inherit private modes.
    os.umask(0o077)
    try:
        directory = configure_environment(arguments.port)
        sys.path.insert(0, str(ROOT))
        from app import app as application
    except ImportError:
        print("缺少本机运行依赖，请先运行一键启动脚本安装依赖。", file=sys.stderr)
        return 1
    except OSError:
        print("无法准备本机数据目录，请检查目录权限与可用空间。", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, sqlite3.Error):
        print("无法加载本机数据或配置；请检查数据库、密钥文件与运行环境。现有文件未改动。", file=sys.stderr)
        return 1
    try:
        server = create_local_server(application, arguments.port)
    except ImportError:
        print("缺少 Waitress，请先运行一键启动脚本安装依赖。", file=sys.stderr)
        return 1
    except OSError as error:
        import errno

        if error.errno in {errno.EADDRINUSE, errno.EACCES} or getattr(error, "winerror", None) in {10048, 10013}:
            print(f"本机端口 {arguments.port} 已被占用或不可用；现有服务未被修改。请使用 --port 指定其他端口。", file=sys.stderr)
        else:
            print("无法准备本机数据目录或启动服务，请检查目录权限与可用空间。", file=sys.stderr)
        return 1
    print(f"本机数据目录：{directory}", flush=True)
    stopped = threading.Event()
    thread = threading.Thread(target=announce_when_ready,
                              args=(f"http://{HOST}:{arguments.port}/", not arguments.no_browser, stopped),
                              name="notebook-readiness", daemon=True)
    thread.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.close()
        server.task_dispatcher.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
