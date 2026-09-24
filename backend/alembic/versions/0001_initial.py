"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("youtube_channel_id", sa.String(64), nullable=False, unique=True),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("avatar_url", sa.String(2048)),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime()),
        sa.Column("next_sync_at", sa.DateTime()),
        sa.Column("video_count", sa.Integer(), nullable=False),
    )
    op.create_table(
        "channel_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("channels.id"), nullable=False, unique=True),
        sa.Column("preferred_languages", sa.JSON(), nullable=False),
        sa.Column("subtitle_preference", sa.String(20), nullable=False),
        sa.Column("allow_translation", sa.Boolean(), nullable=False),
        sa.Column("export_formats", sa.JSON(), nullable=False),
        sa.Column("sync_interval_hours", sa.Integer(), nullable=False),
    )
    op.create_table(
        "videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("youtube_video_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("thumbnail_url", sa.String(2048)),
        sa.Column("video_type", sa.String(20), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "queued", "processing", "completed", "no_subtitle", "language_unavailable", "failed", "blocked", "skipped", name="videostatus"),
            nullable=False,
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_processed_at", sa.DateTime()),
        sa.Column("subtitle_path", sa.String(2048)),
        sa.UniqueConstraint("channel_id", "youtube_video_id", name="uq_channel_video"),
    )
    op.create_table(
        "subtitles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("language", sa.String(128), nullable=False),
        sa.Column("language_code", sa.String(32), nullable=False),
        sa.Column("is_generated", sa.Boolean(), nullable=False),
        sa.Column("is_translatable", sa.Boolean(), nullable=False),
        sa.Column("is_translated", sa.Boolean(), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("file_path", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("video_id", "language_code", "format", name="uq_subtitle_file"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.Enum("queued", "processing", "paused", "completed", "failed", "cancelled", name="jobstatus"), nullable=False),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("channels.id")),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id")),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "job_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id")),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("new_videos", sa.Integer(), nullable=False),
        sa.Column("queued_videos", sa.Integer(), nullable=False),
        sa.Column("successful", sa.Integer(), nullable=False),
        sa.Column("no_subtitle", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("sync_runs")
    op.drop_table("job_logs")
    op.drop_table("jobs")
    op.drop_table("subtitles")
    op.drop_table("videos")
    op.drop_table("channel_settings")
    op.drop_table("channels")
