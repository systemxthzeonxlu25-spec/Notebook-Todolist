#!/usr/bin/env python3
"""使用 SQLite 在线备份 API 创建一致快照，成功后才清理过期备份。"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone


def main() -> int:
    database = Path(os.environ["DATABASE_PATH"]).expanduser().resolve()
    directory = Path(os.environ.get("BACKUP_DIR", "/var/backups/notebook-todolist"))
    retention = int(os.environ.get("BACKUP_RETENTION_DAYS", "14"))
    if not 1 <= retention <= 3650:
        raise ValueError("BACKUP_RETENTION_DAYS 必须为 1 至 3650")
    if not database.is_file():
        raise FileNotFoundError("数据库尚不存在；先启动应用，不能备份空路径")
    if directory.is_symlink():
        raise ValueError("备份目录不能是符号链接")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    directory = directory.resolve()
    os.umask(0o077)
    lock_fd = os.open(
        directory / ".backup.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有备份任务运行，本次跳过。")
            return 0
        filename = "notebook-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
        descriptor, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=directory)
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
            try:
                target = sqlite3.connect(temporary_path)
                try:
                    source.backup(target, pages=256, sleep=0.05)
                    # 快照必须是独立单文件，不能依赖 WAL / SHM 辅助文件。
                    target.execute("PRAGMA journal_mode=DELETE")
                    if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise RuntimeError("备份完整性检查失败")
                finally:
                    target.close()
            finally:
                source.close()
            with temporary_path.open("rb") as snapshot:
                os.fsync(snapshot.fileno())
            temporary_path.replace(directory / filename)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(temporary + suffix).unlink(missing_ok=True)
        cutoff = time.time() - retention * 24 * 60 * 60
        for candidate in directory.iterdir():
            if (
                re.fullmatch(r"notebook-\d{8}T\d{12}Z\.sqlite3", candidate.name)
                and not candidate.is_symlink()
                and candidate.is_file()
                and candidate.stat().st_mtime < cutoff
            ):
                candidate.unlink()
        print(f"SQLite 备份成功：{filename}；保留 {retention} 天。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        print(f"SQLite 备份失败：{error}", file=sys.stderr)
        raise SystemExit(1)
