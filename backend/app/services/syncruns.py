"""SyncRun lifecycle. Counters/status are always recomputed from the run's jobs (the source of truth),
never incremented, so retries, restarts and repeated refreshes cannot double count."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Job, JobStatus, SyncRun, SyncRunStatus

ACTIVE_JOBS = (JobStatus.queued, JobStatus.processing, JobStatus.paused)
# Job.outcome -> SyncRun counter (blocked counts as a failure).
OUTCOME_COLUMN = {"success": "successful", "no_subtitle": "no_subtitle", "language_unavailable": "language_unavailable", "blocked": "failed", "failed": "failed"}


def refresh_sync_run_state(db: Session, run_id: str | None, now: datetime | None = None) -> str | None:
    """Recompute counters, status and finished_at of one run from its jobs. Returns the new status.

    Call it in the same transaction as the job change that may affect the run (after flushing ORM
    changes to jobs). It is idempotent: calling it again with no job change changes nothing.
    """
    if not run_id:
        return None
    run = db.get(SyncRun, run_id, populate_existing=True)
    if not run:
        return None
    now = now or datetime.utcnow()

    scan_statuses = db.scalars(select(Job.status).where(Job.sync_run_id == run_id, Job.kind == "scan")).all()
    scan_active = any(status in ACTIVE_JOBS for status in scan_statuses)
    scan_ok = not scan_statuses or JobStatus.completed in scan_statuses  # legacy runs have no scan job row

    counts = {"successful": 0, "no_subtitle": 0, "language_unavailable": 0, "failed": 0}
    total = active = cancelled = 0
    for status, outcome, n in db.execute(select(Job.status, Job.outcome, func.count()).where(Job.sync_run_id == run_id, Job.kind == "download").group_by(Job.status, Job.outcome)):
        total += n
        if status in ACTIVE_JOBS:
            active += n
        elif status == JobStatus.cancelled:
            cancelled += n
        else:  # terminal; a NULL outcome only occurs on rows finished before outcomes were recorded
            counts[OUTCOME_COLUMN.get(outcome or ("success" if status == JobStatus.completed else "failed"), "failed")] += n

    good = counts["successful"] + counts["no_subtitle"] + counts["language_unavailable"]
    bad = counts["failed"] + cancelled
    if scan_active:
        status = SyncRunStatus.scanning
    elif active:
        status = SyncRunStatus.downloading
    elif not scan_ok:
        status = SyncRunStatus.partial if good else SyncRunStatus.failed
    elif bad == 0:
        status = SyncRunStatus.completed
    else:
        status = SyncRunStatus.partial if good else SyncRunStatus.failed

    for column, value in counts.items():
        setattr(run, column, value)
    run.queued_videos, run.status = total, status.value
    if status in (SyncRunStatus.scanning, SyncRunStatus.downloading):
        run.finished_at = None  # reopened (e.g. a failed job was retried) or still running
    elif run.finished_at is None:
        run.finished_at = now
    if not scan_ok and not scan_active:
        run.error = db.scalar(select(Job.error).where(Job.sync_run_id == run_id, Job.kind == "scan").order_by(Job.created_at.desc()).limit(1)) or run.error
    return run.status
