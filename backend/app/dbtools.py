"""SQLite backup / restore / runtime-log housekeeping. Used by the launchers and as a CLI:

    python -m app.dbtools pre-migrate     backup only if migrations are pending (launcher/Docker)
    python -m app.dbtools backup          backup now
    python -m app.dbtools list            list backups (newest first)
    python -m app.dbtools restore NAME    restore a backup (refuses while the app is running)
    python -m app.dbtools rotate-logs     archive current runtime logs, delete old archives

Backups use SQLite's online backup API, so they are consistent even while the database is in use
(WAL contents are included; no -wal/-shm files are copied).
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import RUNTIME_LOG_DIR, settings

AUTO_RE = re.compile(r"^subtitle-db-\d{8}-\d{6}(-\d+)?\.sqlite3$")
SAFETY_PREFIX = "subtitle-db-prerestore-"
ARCHIVED_LOG_RE = re.compile(r"^(?P<base>[\w.-]+?)\.(?P<stamp>\d{8}-\d{6})\.log$")


class BackupError(RuntimeError): pass
class RestoreError(RuntimeError): pass


def sqlite_path(database_url: str | None = None) -> Path | None:
    url = database_url or settings.database_url
    prefix = "sqlite:///"
    return Path(url[len(prefix):]) if url.startswith(prefix) else None


def _integrity_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try: return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally: conn.close()
    except sqlite3.Error:
        return False


def _has_tables(path: Path) -> bool:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try: return conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
    finally: conn.close()


def database_revision(path: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            return row[0] if row else None
        finally: conn.close()
    except sqlite3.Error:
        return None


def _copy_database(source: Path, destination: Path) -> None:
    """Online-backup `source` into a temp file next to `destination`, verify it, then atomically rename."""
    temp = destination.with_name(destination.name + ".partial")
    try:
        src = sqlite3.connect(str(source)); dst = sqlite3.connect(str(temp))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # the copy is one self-contained file (no -wal/-shm); the app re-enables WAL on connect
        finally: dst.close(); src.close()
        if not _integrity_ok(temp): raise BackupError("Bản sao không qua kiểm tra integrity_check")
        os.replace(temp, destination)
    except BaseException:
        for leftover in (temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")):
            try: leftover.unlink()
            except OSError: pass
        raise


def _unique_name(directory: Path, stem: str, now: datetime) -> Path:
    candidate = directory / f"{stem}{now:%Y%m%d-%H%M%S}.sqlite3"; n = 0
    while candidate.exists():
        n += 1; candidate = directory / f"{stem}{now:%Y%m%d-%H%M%S}-{n}.sqlite3"
    return candidate


def list_backups(directory: Path | None = None) -> list[Path]:
    directory = directory or settings.db_backup_dir
    if not directory.is_dir(): return []
    found = [p for p in directory.iterdir() if p.is_file() and (AUTO_RE.match(p.name) or p.name.startswith(SAFETY_PREFIX)) and p.suffix == ".sqlite3"]
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def prune_backups(directory: Path, keep: int) -> list[Path]:
    """Keep the newest `keep` automatic backups. Pre-restore safety copies are never pruned automatically."""
    auto = [p for p in list_backups(directory) if AUTO_RE.match(p.name)]
    removed = []
    for old in auto[keep:]:
        try: old.unlink(); removed.append(old)
        except OSError: pass
    return removed


def backup_database(db_path: Path | None = None, directory: Path | None = None, keep: int | None = None, now: datetime | None = None) -> Path | None:
    """Returns the backup path, or None when there is nothing to protect (no DB / empty DB).
    Old backups are pruned only after the new one is verified; any failure raises BackupError."""
    db_path = db_path or sqlite_path(); directory = directory or settings.db_backup_dir
    if db_path is None or not db_path.is_file() or db_path.stat().st_size == 0: return None
    try:
        if not _has_tables(db_path): return None
        directory.mkdir(parents=True, exist_ok=True)
        target = _unique_name(directory, "subtitle-db-", now or datetime.now())
        _copy_database(db_path, target)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"Không tạo được backup: {type(exc).__name__}: {exc}") from exc
    prune_backups(directory, keep or settings.db_backup_keep_count)
    return target


def migrations_pending(db_path: Path) -> bool:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    return database_revision(db_path) != ScriptDirectory.from_config(config).get_current_head()


def pre_migration_backup(db_path: Path | None = None, **kwargs) -> Path | None:
    """Back up an existing database only when `alembic upgrade head` would actually change it,
    so routine restarts do not rotate the one backup that matters out of the retention window."""
    db_path = db_path or sqlite_path()
    if db_path is None or not db_path.is_file() or not migrations_pending(db_path): return None
    return backup_database(db_path, **kwargs)


# ---------- restore ----------
def assert_database_idle(db_path: Path) -> None:
    """Refuse to restore while any process may be using the database."""
    if not db_path.exists(): return
    try:  # a live worker refreshes its heartbeat every few seconds
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT max(last_heartbeat_at) FROM worker_heartbeats").fetchone()
            if row and row[0]:
                last = datetime.fromisoformat(row[0])
                if datetime.utcnow() - last < timedelta(seconds=settings.worker_stale_seconds): raise RestoreError("Worker vẫn đang chạy; hãy chạy stop-app.bat trước.")
        except sqlite3.OperationalError: pass  # older DB without the heartbeat table
        finally: conn.close()
        lock = sqlite3.connect(str(db_path), timeout=0.5)
        try: lock.execute("BEGIN EXCLUSIVE"); lock.rollback()
        finally: lock.close()
    except sqlite3.OperationalError as exc:
        raise RestoreError("Database đang được sử dụng; hãy dừng ứng dụng trước.") from exc
    probe = db_path.with_name(db_path.name + ".probe")  # Windows refuses to rename a file another process holds open
    try: os.rename(db_path, probe); os.rename(probe, db_path)
    except OSError as exc:
        raise RestoreError("File database đang bị tiến trình khác giữ; hãy dừng ứng dụng trước.") from exc


def resolve_backup(name: str, directory: Path | None = None) -> Path:
    directory = (directory or settings.db_backup_dir).resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory or candidate.suffix != ".sqlite3":  # names only: no path traversal
        raise RestoreError("Tên backup không hợp lệ")
    return candidate


def restore_database(backup: Path, db_path: Path | None = None, directory: Path | None = None, now: datetime | None = None) -> dict:
    """Validate first, keep a safety copy of the current DB, then swap the restored file in.

    Nothing about the current database is touched until the chosen backup has been verified.
    """
    db_path = db_path or sqlite_path(); directory = directory or settings.db_backup_dir
    if db_path is None: raise RestoreError("Chỉ hỗ trợ SQLite")
    if not backup.is_file(): raise RestoreError("Không tìm thấy file backup")
    try:
        if not _integrity_ok(backup) or not _has_tables(backup): raise RestoreError("Backup không hợp lệ hoặc bị hỏng")
    except sqlite3.Error as exc:
        raise RestoreError("Backup không hợp lệ hoặc bị hỏng") from exc
    assert_database_idle(db_path)

    safety = None
    if db_path.is_file() and db_path.stat().st_size > 0:
        try:
            if _has_tables(db_path):
                directory.mkdir(parents=True, exist_ok=True)
                safety = _unique_name(directory, SAFETY_PREFIX, now or datetime.now()); _copy_database(db_path, safety)
        except (sqlite3.Error, OSError) as exc:
            raise RestoreError(f"Không tạo được backup an toàn cho DB hiện tại; hủy restore: {exc}") from exc

    db_path.parent.mkdir(parents=True, exist_ok=True)
    staged = db_path.with_name(db_path.name + ".restoring")
    try:
        _copy_database(backup, staged)  # same-directory temp file => atomic os.replace below
        for suffix in ("-wal", "-shm"):  # leftovers of the old DB must not be replayed onto the restored one
            try: Path(str(db_path) + suffix).unlink()
            except FileNotFoundError: pass
        os.replace(staged, db_path)
    except BaseException:
        try: staged.unlink()
        except OSError: pass
        raise
    if not _integrity_ok(db_path): raise RestoreError("DB sau restore không qua integrity_check")
    return {"restored_from": backup.name, "safety_backup": safety.name if safety else None, "revision": database_revision(db_path)}


# ---------- runtime logs ----------
def rotate_logs(log_dir: Path | None = None, retention_days: int | None = None, now: datetime | None = None) -> dict:
    """Archive the previous run's logs under a timestamped name (instead of overwriting them) and
    delete archives older than the retention. Files that are still in use are left alone."""
    log_dir = log_dir or RUNTIME_LOG_DIR; retention_days = retention_days or settings.runtime_log_retention_days
    now = now or datetime.now(); archived = deleted = 0
    if not log_dir.is_dir(): return {"archived": 0, "deleted": 0}
    for path in list(log_dir.glob("*.log")):
        if ARCHIVED_LOG_RE.match(path.name) or path.stat().st_size == 0: continue
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
        try: path.rename(path.with_name(f"{path.stem}.{stamp:%Y%m%d-%H%M%S}.log")); archived += 1
        except OSError: pass  # still open by a running process: never touch the current log
    cutoff = now - timedelta(days=retention_days)
    for path in list(log_dir.glob("*.log")):
        match = ARCHIVED_LOG_RE.match(path.name)
        if match and datetime.strptime(match["stamp"], "%Y%m%d-%H%M%S") < cutoff:
            try: path.unlink(); deleted += 1
            except OSError: pass
    return {"archived": archived, "deleted": deleted}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.dbtools")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pre-migrate", "backup", "list", "rotate-logs"): sub.add_parser(name)
    restore = sub.add_parser("restore"); restore.add_argument("name")
    args = parser.parse_args(argv)
    try:
        if args.command in ("backup", "pre-migrate"):
            path = (backup_database if args.command == "backup" else pre_migration_backup)()
            print(f"Backup: {path.name}" if path else "Không cần backup.")
        elif args.command == "list":
            for p in list_backups(): print(p.name)
        elif args.command == "rotate-logs": print(rotate_logs())
        else:
            info = restore_database(resolve_backup(args.name))
            print(f"Đã restore {info['restored_from']} (revision {info['revision']}); DB cũ được lưu tại {info['safety_backup'] or '(không có DB cũ)'}.")
    except (BackupError, RestoreError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
