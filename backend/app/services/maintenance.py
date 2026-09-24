"""Bounded-growth housekeeping. Only terminal history is ever deleted; business data (channels,
videos, subtitles) and anything queued/processing/paused or belonging to an active SyncRun is kept."""

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Job, JobLog, JobStatus, SyncRun, SyncRunStatus
from .workers import cleanup_worker_records

logger = logging.getLogger("app.maintenance")
BATCH = 500
TERMINAL_JOBS = (JobStatus.completed, JobStatus.failed, JobStatus.cancelled)
TERMINAL_RUNS = (SyncRunStatus.completed.value, SyncRunStatus.partial.value, SyncRunStatus.failed.value)


def _prune_job_logs(db: Session, cutoff: datetime) -> int:
    """Old logs, except those of jobs that are still active (they may be debugged right now)."""
    total = 0
    active = select(Job.id).where(Job.status.notin_(TERMINAL_JOBS))
    while True:
        ids = db.scalars(select(JobLog.id).where(JobLog.created_at < cutoff, (JobLog.job_id.is_(None)) | (JobLog.job_id.notin_(active))).limit(BATCH)).all()
        if not ids:
            return total
        db.execute(delete(JobLog).where(JobLog.id.in_(ids)))
        db.commit()
        total += len(ids)


def _delete_jobs(db: Session, job_ids: list[str]):
    db.execute(delete(JobLog).where(JobLog.job_id.in_(job_ids)))  # FK: logs first
    db.execute(delete(Job).where(Job.id.in_(job_ids), Job.status.in_(TERMINAL_JOBS)))


def _prune_runs(db: Session, cutoff: datetime) -> int:
    """A finished run goes together with all of its (terminal) jobs, so counters of surviving runs never change."""
    total = 0
    while True:
        run_ids = db.scalars(
            select(SyncRun.id)
            .where(SyncRun.status.in_(TERMINAL_RUNS), SyncRun.finished_at.is_not(None), SyncRun.finished_at < cutoff)
            .where(~exists().where(Job.sync_run_id == SyncRun.id, Job.status.notin_(TERMINAL_JOBS)))
            .limit(BATCH)
        ).all()
        if not run_ids:
            return total
        job_ids = db.scalars(select(Job.id).where(Job.sync_run_id.in_(run_ids))).all()
        for start in range(0, len(job_ids), BATCH):
            _delete_jobs(db, job_ids[start : start + BATCH])
        # re-check inside the same transaction: only runs with no remaining jobs may go (FK jobs.sync_run_id)
        gone = db.execute(delete(SyncRun).where(SyncRun.id.in_(run_ids), SyncRun.status.in_(TERMINAL_RUNS), ~exists().where(Job.sync_run_id == SyncRun.id)))
        db.commit()
        total += gone.rowcount
        if gone.rowcount == 0:
            return total  # nothing deletable in this batch; avoid spinning


def _prune_standalone_jobs(db: Session, cutoff: datetime) -> int:
    """Terminal jobs outside any run (manual downloads, pre-linkage history)."""
    total = 0
    while True:
        ids = db.scalars(
            select(Job.id)
            .where(Job.sync_run_id.is_(None), Job.status.in_(TERMINAL_JOBS), Job.created_at < cutoff, (Job.finished_at.is_(None)) | (Job.finished_at < cutoff))
            .limit(BATCH)
        ).all()
        if not ids:
            return total
        _delete_jobs(db, ids)
        db.commit()
        total += len(ids)


def run_retention(db: Session, now: datetime | None = None) -> dict:
    """Idempotent; each batch is its own short transaction so it never blocks the API/worker for long."""
    now = now or datetime.utcnow()
    history = now - timedelta(days=settings.job_history_retention_days)
    result = {
        "runs": _prune_runs(db, history),
        "jobs": _prune_standalone_jobs(db, history),
        "job_logs": _prune_job_logs(db, now - timedelta(days=settings.job_log_retention_days)),
        "worker_records": cleanup_worker_records(db, now),
    }
    if any(result.values()):
        logger.info("Retention: %s", result)
    return result


class MaintenanceSchedule:
    """`due()` is true at startup and then every MAINTENANCE_INTERVAL_HOURS - never per poll."""

    def __init__(self, clock=time.monotonic):
        self._clock, self._last = clock, None

    def due(self) -> bool:
        return self._last is None or self._clock() - self._last >= settings.maintenance_interval_hours * 3600

    def done(self):
        self._last = self._clock()
