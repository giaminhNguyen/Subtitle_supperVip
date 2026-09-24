"""Health + diagnostics. Never calls YouTube, never returns secrets or absolute paths."""
import os
import platform
import shutil
import tempfile
import time
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import RUNTIME_LOG_DIR, settings
from ..models import Job, JobStatus, SyncRun, SyncRunStatus
from .runtime_settings import youtube_api_key_configured
from .workers import worker_summary

APP_VERSION = "0.1.0"
STARTED_AT = time.time()


def check_database(db: Session) -> str:
    try: db.execute(text("SELECT 1")); return "ok"
    except Exception: return "error"


def check_storage(write_probe: bool = False) -> str:
    """Cheap by default (exists + os.access); diagnostics adds a real create/delete probe."""
    try:
        root = settings.data_dir
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK): return "error"
        if write_probe:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".health-", delete=True): pass
        return "ok"
    except OSError:
        return "error"


def health_summary(db: Session) -> tuple[dict, int]:
    """Critical (503): database or storage failing. Degraded (200): worker offline/absent - the API still serves."""
    database, storage = check_database(db), check_storage()
    worker = worker_summary(db) if database == "ok" else {"status": "unknown", "active": 0, "stale": 0, "last_heartbeat": None, "busy": 0}
    key = youtube_api_key_configured(db) if database == "ok" else False
    critical = database != "ok" or storage != "ok"
    degraded = worker["status"] != "running" or not key
    status = "critical" if critical else ("degraded" if degraded else "ok")
    body = {"ok": not critical, "status": status, "api": "ok", "database": database, "storage": storage, "worker": worker, "youtube_api_key_configured": key}
    return body, (503 if critical else 200)


def _revision(db: Session) -> str | None:
    try: return db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception: return None


def diagnostics(db: Session) -> dict:
    now = datetime.utcnow()
    database = check_database(db)
    info = {"app": {"version": APP_VERSION, "python": platform.python_version(), "uptime_seconds": int(time.time() - STARTED_AT)},
            "database": {"status": database, "engine": db.get_bind().dialect.name}}
    if database == "ok":
        info["database"]["revision"] = _revision(db)
        if db.get_bind().dialect.name == "sqlite":
            info["database"]["journal_mode"] = db.execute(text("PRAGMA journal_mode")).scalar()
            path = db.get_bind().url.database
            info["database"]["size_bytes"] = os.path.getsize(path) if path and os.path.exists(path) else None
    storage = {"status": check_storage(write_probe=True)}
    try:
        usage = shutil.disk_usage(settings.data_dir); storage.update(free_bytes=usage.free, total_bytes=usage.total)
    except OSError: storage.update(free_bytes=None, total_bytes=None)
    info["storage"] = storage
    info["runtime_logs"] = ".runtime/logs" if RUNTIME_LOG_DIR.is_dir() else None
    if database != "ok": return info
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())  # indexed status column
    info["queue"] = {status.value: counts.get(status, 0) for status in JobStatus}
    info["worker"] = worker_summary(db, now)
    info["youtube_api_key_configured"] = youtube_api_key_configured(db)
    info["active_sync_runs"] = [{"id": r.id, "channel_id": r.channel_id, "mode": r.mode, "status": r.status, "started_at": r.started_at.isoformat(), "queued": r.queued_videos,
                                 "successful": r.successful, "failed": r.failed}
                                for r in db.scalars(select(SyncRun).where(SyncRun.status.in_([SyncRunStatus.scanning.value, SyncRunStatus.downloading.value])).order_by(SyncRun.started_at.desc()).limit(20))]
    last_run = db.scalar(select(func.max(SyncRun.finished_at)).where(SyncRun.status == SyncRunStatus.completed.value))
    info["last_successful_sync"] = last_run.isoformat() if last_run else None
    return info
