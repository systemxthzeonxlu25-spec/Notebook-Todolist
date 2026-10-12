"""Run the VPS backup command against real SQLite files and process locks."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

fcntl = pytest.importorskip("fcntl", reason="VPS backup uses Linux/POSIX file locks")
BACKUP_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup_sqlite.py"


@pytest.fixture
def backup_script():
    assert BACKUP_SCRIPT.is_file(), "The deployment backup script must exist in deploy/backup_sqlite.py"
    return BACKUP_SCRIPT


def run_backup(script, database, directory, retention=14):
    environment = {**os.environ, "DATABASE_PATH": str(database), "BACKUP_DIR": str(directory),
                   "BACKUP_RETENTION_DAYS": str(retention)}
    return subprocess.run([sys.executable, str(script)], env=environment, capture_output=True,
                          text=True, timeout=15, check=False)


def create_database(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE backup_check (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO backup_check VALUES (?,?)", (1, "笔记、待办与复习卡"))
        connection.commit()


def mark_old(path, days=30):
    timestamp = time.time() - days * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def snapshots(directory):
    return sorted(directory.glob("notebook-*.sqlite3"))


def test_live_wal_backup_is_consistent_and_independently_restorable(backup_script, tmp_path):
    database, directory = tmp_path / "live.sqlite3", tmp_path / "backups"
    with closing(sqlite3.connect(database)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE backup_check (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        committed = [(number, f"已提交笔记 {number}") for number in range(1, 101)]
        writer.executemany("INSERT INTO backup_check VALUES (?,?)", committed)
        writer.commit()
        # Keep a real WAL writer transaction open during the external backup.
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO backup_check VALUES (?,?)", (101, "尚未提交，不能进入快照"))
        wal = Path(str(database) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        result = run_backup(backup_script, database, directory)
        assert result.returncode == 0, result.stderr
        files = snapshots(directory)
        assert len(files) == 1
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        assert stat.S_IMODE((directory / ".backup.lock").stat().st_mode) == 0o600
        assert not list(directory.glob("*.tmp*"))
        assert not list(directory.glob("*-wal"))
        assert not list(directory.glob("*-shm"))
        restored_directory = tmp_path / "independent-restore"
        restored_directory.mkdir()
        restored = restored_directory / "restored.sqlite3"
        shutil.copyfile(files[0], restored)
        # immutable=1 disables journal/WAL discovery: only this copied file exists.
        with closing(sqlite3.connect(restored.as_uri() + "?mode=ro&immutable=1", uri=True)) as restored_db:
            assert restored_db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored_db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert restored_db.execute("SELECT id,content FROM backup_check ORDER BY id").fetchall() == committed
        writer.rollback()


def test_retention_only_removes_old_matching_regular_snapshot_files(backup_script, tmp_path):
    database, directory = tmp_path / "data.sqlite3", tmp_path / "backups"
    create_database(database)
    directory.mkdir(mode=0o755)
    expired = directory / "notebook-20000101T000000000000Z.sqlite3"
    expired.write_bytes(b"expired snapshot")
    mark_old(expired)
    recent = directory / "notebook-20010101T000000000000Z.sqlite3"
    recent.write_bytes(b"recent snapshot despite old-looking name")
    unrelated = [directory / name for name in (
        "manual.sqlite3", "notes.txt", "notebook-archive.sqlite3", ".backup-unrelated.tmp",
        "notebook-20000101T000000Z.sqlite3",
    )]
    for path in unrelated:
        path.write_bytes(b"unrelated file")
        mark_old(path)
    matching_directory = directory / "notebook-20020101T000000000000Z.sqlite3"
    matching_directory.mkdir()
    mark_old(matching_directory)
    external = tmp_path / "external-original.sqlite3"
    external.write_bytes(b"must preserve external target")
    link = directory / "notebook-20030101T000000000000Z.sqlite3"
    link.symlink_to(external)
    mark_old(link)
    result = run_backup(backup_script, database, directory, retention=7)
    assert result.returncode == 0, result.stderr
    assert not expired.exists()
    assert recent.read_bytes() == b"recent snapshot despite old-looking name"
    assert all(path.read_bytes() == b"unrelated file" for path in unrelated)
    assert matching_directory.is_dir()
    assert link.is_symlink() and external.read_bytes() == b"must preserve external target"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("source_state", ["missing", "corrupt"])
def test_failed_backup_preserves_old_snapshots_and_removes_partial_outputs(backup_script, tmp_path, source_state):
    database, directory = tmp_path / "data.sqlite3", tmp_path / "backups"
    if source_state == "corrupt":
        database.write_bytes(b"this is not a SQLite database")
    directory.mkdir()
    previous = directory / "notebook-20000101T000000000000Z.sqlite3"
    previous.write_bytes(b"last surviving backup")
    mark_old(previous)
    result = run_backup(backup_script, database, directory, retention=1)
    assert result.returncode == 1
    assert "备份失败" in result.stderr
    assert previous.read_bytes() == b"last surviving backup"
    assert snapshots(directory) == [previous]
    assert not list(directory.glob(".backup-*.tmp*"))
    if source_state == "missing":
        assert not database.exists(), "A missing source must never become a new empty database"
    else:
        assert database.read_bytes() == b"this is not a SQLite database"


def test_concurrent_process_lock_skips_backup_and_retention(backup_script, tmp_path):
    database, directory = tmp_path / "data.sqlite3", tmp_path / "backups"
    create_database(database)
    directory.mkdir(mode=0o700)
    previous = directory / "notebook-20000101T000000000000Z.sqlite3"
    previous.write_bytes(b"retain while another job is running")
    mark_old(previous)
    descriptor = os.open(directory / ".backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_backup(backup_script, database, directory, retention=1)
        assert result.returncode == 0, result.stderr
        assert "跳过" in result.stdout
        assert snapshots(directory) == [previous]
        assert previous.read_bytes() == b"retain while another job is running"
        assert not list(directory.glob(".backup-*.tmp*"))
    # Releasing the real process lock lets the next invocation finish normally.
    result = run_backup(backup_script, database, directory, retention=1)
    assert result.returncode == 0, result.stderr
    assert "备份成功" in result.stdout
    assert not previous.exists()
    assert len(snapshots(directory)) == 1
