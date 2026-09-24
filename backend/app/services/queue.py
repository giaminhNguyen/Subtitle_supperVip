"""DB-backed job queue primitives: atomic claim, lease heartbeat, stale-job recovery."""

import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Job, JobStatus
from .jobs import log
from .syncruns import refresh_sync_run_state

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
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.queued)
            .values(
                status=JobStatus.processing,
                worker_id=worker_id,
                started_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=Job.attempts + 1,
            )
            .execution_options(synchronize_session=False)
        )
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
        values = (
            dict(status=JobStatus.failed, finished_at=now, error="Worker dừng đột ngột và đã hết số lần thử", outcome="failed" if job.kind == "download" else None)
            if exhausted
            else dict(status=JobStatus.queued, scheduled_at=now)
        )
        # Re-check the lease in the WHERE clause: a heartbeat may have renewed it since the SELECT.
        result = db.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == JobStatus.processing, (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now))
            .values(worker_id=None, lease_expires_at=None, **values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            recovered += 1
            log(db, job, "Job hết hạn lease; đánh dấu thất bại" if exhausted else "Khôi phục job bị gián đoạn (lease hết hạn)", "error" if exhausted else "warning")
            if exhausted and job.video_id:
                from ..models import Video, VideoStatus

                video = db.get(Video, job.video_id)
                if video:
                    video.status = VideoStatus.failed
    db.flush()
    for run_id in {job.sync_run_id for job in stale if job.sync_run_id}:  # jobs failed above may close their run
        refresh_sync_run_state(db, run_id)
    db.commit()
    return recovered


class LostOwnership(Exception):
    """This worker no longer holds the job (lease expired and it was recovered/re-claimed)."""


def guard_job(db: Session, job_id: str, owner: str, attempts: int, **values) -> None:
    """Compare-and-set on the job row: applies `values` only if `owner` still owns this exact claim (worker id + attempt number,
    both captured at claim time so a rollback/expire cannot change what we compare against).

    The UPDATE takes SQLite's write lock, so when it succeeds nobody can take the job away before
    the surrounding transaction commits. Callers put all result writes *after* this call in the
    same transaction; on LostOwnership they must roll back.
    """
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.processing, Job.worker_id == owner, Job.attempts == attempts)
        .values(values or {"worker_id": owner})
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise LostOwnership(job_id)


class Heartbeat:
    """Background thread that keeps a job's lease alive while the worker is busy (uses its own session)."""

    def __init__(self, session_factory, job_id: str, worker_id: str, lease_seconds: int, max_runtime_seconds: float = float("inf")):
        self._factory, self._job_id, self._worker_id, self._lease = session_factory, job_id, worker_id, lease_seconds
        self._max_runtime, self._started = max_runtime_seconds, time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"heartbeat-{job_id[:8]}")

    def beat(self) -> bool:
        db = self._factory()
        try:
            result = db.execute(
                update(Job)
                .where(Job.id == self._job_id, Job.worker_id == self._worker_id, Job.status == JobStatus.processing)
                .values(lease_expires_at=datetime.utcnow() + timedelta(seconds=self._lease))
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def _run(self):
        while not self._stop.wait(max(1.0, self._lease / 3)):
            if time.monotonic() - self._started > self._max_runtime:
                logger.error("Job %s chạy quá thời gian tối đa; ngừng gia hạn lease để job có thể bị thu hồi", self._job_id)
                return
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
