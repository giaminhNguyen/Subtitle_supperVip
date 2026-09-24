"""DB-backed job queue primitives: atomic claim, lease heartbeat, stale-job recovery."""
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Job, JobStatus
from .jobs import log

logger = logging.getLogger("app.queue")


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def claim_next_job(db: Session, worker_id: str, lease_seconds: int, now: datetime | None = None) -> Job | None:
    """Atomically move one due queued job to processing for `worker_id`.

    The conditional UPDATE (status must still be 'queued') is the compare-and-set: of two
    workers racing for the same job, exactly one sees rowcount == 1.
    """
    now = now or datetime.utcnow()
    for _ in range(10):
        job_id = db.scalar(select(Job.id).where(Job.status == JobStatus.queued, Job.scheduled_at <= now).order_by(Job.created_at).limit(1))
        if job_id is None:
            return None
        claimed = db.execute(update(Job).where(Job.id == job_id, Job.status == JobStatus.queued).values(
            status=JobStatus.processing, worker_id=worker_id, started_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds), attempts=Job.attempts + 1,
        ).execution_options(synchronize_session=False))
        if claimed.rowcount == 1:
            job = db.scalar(select(Job).where(Job.id == job_id).execution_options(populate_existing=True))
            log(db, job, f"Bắt đầu lần thử {job.attempts}")
            db.commit()
            return job
        db.rollback()  # lost the race; try the next candidate
    return None


def recover_stale_jobs(db: Session, now: datetime | None = None) -> int:
    """Requeue (or fail, once attempts are exhausted) jobs whose lease expired. Live leases are untouched."""
    now = now or datetime.utcnow()
    stale = db.scalars(select(Job).where(Job.status == JobStatus.processing, (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now))).all()
    recovered = 0
    for job in stale:
        exhausted = job.attempts >= job.max_attempts
        values = dict(status=JobStatus.failed, finished_at=now, error="Worker dừng đột ngột và đã hết số lần thử") if exhausted else dict(status=JobStatus.queued, scheduled_at=now)
        # Re-check the lease in the WHERE clause: a heartbeat may have renewed it since the SELECT.
        result = db.execute(update(Job).where(Job.id == job.id, Job.status == JobStatus.processing, (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now)).values(worker_id=None, lease_expires_at=None, **values).execution_options(synchronize_session=False))
        if result.rowcount == 1:
            recovered += 1
            log(db, job, "Job hết hạn lease; đánh dấu thất bại" if exhausted else "Khôi phục job bị gián đoạn (lease hết hạn)", "error" if exhausted else "warning")
            if exhausted and job.video_id:
                from ..models import Video, VideoStatus
                video = db.get(Video, job.video_id)
                if video: video.status = VideoStatus.failed
    db.commit()
    return recovered


def owns_job(session_factory, job_id: str, worker_id: str) -> bool:
    """Fresh-session check that this worker still holds the job (lease not lost to recovery)."""
    db = session_factory()
    try:
        return db.scalar(select(Job.id).where(Job.id == job_id, Job.status == JobStatus.processing, Job.worker_id == worker_id)) is not None
    finally:
        db.close()


class Heartbeat:
    """Background thread that keeps a job's lease alive while the worker is busy (uses its own session)."""

    def __init__(self, session_factory, job_id: str, worker_id: str, lease_seconds: int):
        self._factory, self._job_id, self._worker_id, self._lease = session_factory, job_id, worker_id, lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"heartbeat-{job_id[:8]}")

    def beat(self) -> bool:
        db = self._factory()
        try:
            result = db.execute(update(Job).where(Job.id == self._job_id, Job.worker_id == self._worker_id, Job.status == JobStatus.processing)
                                .values(lease_expires_at=datetime.utcnow() + timedelta(seconds=self._lease)))
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def _run(self):
        while not self._stop.wait(max(1.0, self._lease / 3)):
            try:
                if not self.beat():
                    logger.warning("Lease của job %s đã mất", self._job_id)
                    return
            except Exception:  # e.g. transient "database is locked"; the next beat retries
                logger.exception("Heartbeat lỗi")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=5)
