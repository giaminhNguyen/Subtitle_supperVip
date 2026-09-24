"""Worker liveness: each worker process keeps one row in worker_heartbeats fresh.

This is separate from job leases (which protect one job). A heartbeat only counts as "alive" while the
worker's main loop is demonstrably making progress: the heartbeat thread stops writing when the loop
has stalled while idle, or a job has run longer than JOB_MAX_RUNTIME_SECONDS, so a hung worker turns
stale instead of looking healthy.
"""
import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import WorkerHeartbeat

logger = logging.getLogger("app.worker")


class WorkerState:
    """In-process view of what the main loop is doing; read by the heartbeat thread."""

    def __init__(self, clock=time.monotonic):
        self._clock, self._lock = clock, threading.Lock()
        self.state, self.job_id = "idle", None
        self.since = self.loop_at = clock()

    def tick(self):
        """Main loop is alive (called every iteration)."""
        with self._lock: self.loop_at = self._clock()

    def busy(self, job_id: str | None):
        with self._lock: self.state, self.job_id, self.since, self.loop_at = "busy", job_id, self._clock(), self._clock()

    def idle(self):
        with self._lock: self.state, self.job_id, self.since, self.loop_at = "idle", None, self._clock(), self._clock()

    def snapshot(self) -> tuple[str, str | None, bool]:
        """(state, job_id, progressing) - progressing is False for a stalled idle loop or an over-long job."""
        with self._lock:
            age = self._clock() - (self.since if self.state == "busy" else self.loop_at)
            limit = settings.job_max_runtime_seconds if self.state == "busy" else settings.worker_stale_seconds
            return self.state, self.job_id, age <= limit


STATE = WorkerState()


def write_heartbeat(db: Session, worker_id: str, started_at: datetime, state: str, job_id: str | None, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    values = dict(hostname=socket.gethostname(), pid=os.getpid(), last_heartbeat_at=now, state=state, current_job_id=job_id)
    insert = sqlite_insert(WorkerHeartbeat).values(worker_id=worker_id, started_at=started_at, **values)
    db.execute(insert.on_conflict_do_update(index_elements=["worker_id"], set_=values))  # started_at is kept from the first beat
    db.commit()


class HeartbeatThread:
    """Writes this worker's heartbeat every WORKER_HEARTBEAT_SECONDS while the main loop is progressing."""

    def __init__(self, session_factory, worker_id: str, state: WorkerState = STATE):
        self._factory, self._worker_id, self._state = session_factory, worker_id, state
        self._started_at = datetime.utcnow()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="worker-heartbeat")

    def beat_once(self) -> bool:
        state, job_id, progressing = self._state.snapshot()
        if not progressing: return False
        with self._factory() as db: write_heartbeat(db, self._worker_id, self._started_at, state, job_id)
        return True

    def _run(self):
        while True:
            try:
                if not self.beat_once(): logger.error("Worker loop không tiến triển; ngừng ghi heartbeat")
            except Exception:  # e.g. "database is locked": the next beat retries
                logger.exception("Không ghi được heartbeat")
            if self._stop.wait(settings.worker_heartbeat_seconds): return

    def start(self):
        self._thread.start(); return self

    def stop(self):
        self._stop.set(); self._thread.join(timeout=5)
        try:
            with self._factory() as db:  # best effort: a clean shutdown should not look like a crash
                db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == self._worker_id)); db.commit()
        except Exception: pass


def worker_summary(db: Session, now: datetime | None = None) -> dict:
    """Cheap: the table holds one row per recent worker process."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=settings.worker_stale_seconds)
    rows = db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_heartbeat_at.desc())).all()
    active = [r for r in rows if r.last_heartbeat_at >= cutoff]
    status = "running" if active else ("offline" if rows else "absent")
    return {"status": status, "active": len(active), "stale": len(rows) - len(active),
            "last_heartbeat": (rows[0].last_heartbeat_at.isoformat() if rows else None),
            "busy": sum(1 for r in active if r.state == "busy")}


def cleanup_worker_records(db: Session, now: datetime | None = None) -> int:
    """Drop heartbeat rows that have been dead for WORKER_RECORD_RETENTION_DAYS (days, so small clock skew is irrelevant)."""
    now = now or datetime.utcnow()
    result = db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_heartbeat_at < now - timedelta(days=settings.worker_record_retention_days)))
    db.commit()
    return result.rowcount
