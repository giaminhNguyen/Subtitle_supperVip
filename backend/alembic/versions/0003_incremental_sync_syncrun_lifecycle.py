"""incremental sync state, SyncRun lifecycle, job/sync linkage, indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25
"""
import json
import logging

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('queued','processing','paused')"
log = logging.getLogger("alembic.runtime.migration")


def upgrade():
    bind = op.get_bind()
    # Channels: cached playlist + incremental state. All NULL for legacy rows, which makes their first
    # `sync new` a full scan (correct, just not cheap); nothing here touches the network.
    op.add_column("channels", sa.Column("uploads_playlist_id", sa.String(128)))
    op.add_column("channels", sa.Column("sync_cursor_video_id", sa.String(32)))
    op.add_column("channels", sa.Column("history_complete_at", sa.DateTime()))

    op.add_column("sync_runs", sa.Column("language_unavailable", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sync_runs", sa.Column("status", sa.String(20), nullable=False, server_default="completed"))
    op.add_column("sync_runs", sa.Column("error", sa.Text()))
    # SQLite cannot ADD CONSTRAINT later, and a batch table rebuild would trip FKs from job_logs -> jobs.
    op.execute("ALTER TABLE jobs ADD COLUMN sync_run_id VARCHAR(36) REFERENCES sync_runs(id)")
    op.add_column("jobs", sa.Column("outcome", sa.String(24)))

    # Legacy SyncRuns were marked finished right after enqueueing; keep them as historical records.
    bind.execute(sa.text("UPDATE sync_runs SET status = CASE WHEN finished_at IS NULL THEN 'failed' ELSE 'completed' END"))
    runs = {row[0] for row in bind.execute(sa.text("SELECT id FROM sync_runs"))}
    for job_id, payload in bind.execute(sa.text("SELECT id, payload FROM jobs")).fetchall():
        try:
            run_id = (json.loads(payload) if isinstance(payload, str) else payload or {}).get("sync_run_id")
        except (ValueError, AttributeError):
            continue
        if run_id in runs:
            bind.execute(sa.text("UPDATE jobs SET sync_run_id = :run WHERE id = :id"), {"run": run_id, "id": job_id})
    # Outcome of already-finished download jobs, from what their video ended as.
    bind.execute(sa.text("""UPDATE jobs SET outcome = CASE
        WHEN status = 'completed' THEN COALESCE((SELECT CASE v.status WHEN 'no_subtitle' THEN 'no_subtitle' WHEN 'language_unavailable' THEN 'language_unavailable' ELSE 'success' END FROM videos v WHERE v.id = jobs.video_id), 'success')
        WHEN status = 'failed' THEN COALESCE((SELECT CASE v.status WHEN 'blocked' THEN 'blocked' ELSE 'failed' END FROM videos v WHERE v.id = jobs.video_id), 'failed')
        END WHERE kind = 'download' AND status IN ('completed','failed')"""))

    op.create_index("ix_jobs_sync_run_id", "jobs", ["sync_run_id"])
    op.create_index("ix_jobs_claim", "jobs", ["status", "scheduled_at", "created_at"])
    op.create_index("ix_videos_channel_published", "videos", ["channel_id", "published_at"])
    op.create_index("ix_sync_runs_channel_status", "sync_runs", ["channel_id", "status"])

    # Partial unique indexes make "one active job per target" a DB guarantee. Never delete legacy
    # duplicates to make them fit: if any exist, skip the index (enqueue() still de-duplicates in code).
    for name, columns, where in (("uq_active_job_video", "kind, video_id", f"video_id IS NOT NULL AND {ACTIVE}"),
                                 ("uq_active_job_channel", "kind, channel_id", f"video_id IS NULL AND {ACTIVE}")):
        duplicates = bind.execute(sa.text(f"SELECT 1 FROM jobs WHERE {where} GROUP BY {columns} HAVING count(*) > 1 LIMIT 1")).first()
        if duplicates:
            log.warning("Skipping %s: duplicate active jobs exist in this database", name)
        else:
            bind.execute(sa.text(f"CREATE UNIQUE INDEX {name} ON jobs ({columns}) WHERE {where}"))


def downgrade():
    for name in ("uq_active_job_channel", "uq_active_job_video"):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.drop_index("ix_sync_runs_channel_status", "sync_runs")
    op.drop_index("ix_videos_channel_published", "videos")
    op.drop_index("ix_jobs_claim", "jobs")
    op.drop_index("ix_jobs_sync_run_id", "jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("outcome")
        batch.drop_column("sync_run_id")
    with op.batch_alter_table("sync_runs") as batch:
        for column in ("error", "status", "language_unavailable"): batch.drop_column(column)
    with op.batch_alter_table("channels") as batch:
        for column in ("history_complete_at", "sync_cursor_video_id", "uploads_playlist_id"): batch.drop_column(column)
