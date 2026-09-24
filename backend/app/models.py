import enum
import uuid
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


def uid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.utcnow()


class VideoStatus(str, enum.Enum):
    pending = "pending"; queued = "queued"; processing = "processing"; completed = "completed"
    no_subtitle = "no_subtitle"; language_unavailable = "language_unavailable"; failed = "failed"
    blocked = "blocked"; skipped = "skipped"


class JobStatus(str, enum.Enum):
    queued = "queued"; processing = "processing"; paused = "paused"; completed = "completed"; failed = "failed"; cancelled = "cancelled"


class SyncRunStatus(str, enum.Enum):
    scanning = "scanning"; downloading = "downloading"; completed = "completed"; partial = "partial"; failed = "failed"


ACTIVE_JOB_SQL = "status IN ('queued','processing','paused')"


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    youtube_channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512))
    avatar_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    video_count: Mapped[int] = mapped_column(Integer, default=0)
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Incremental sync state: cursor = newest playlist entry at the last verified scan;
    # history_complete_at = when a scan last verified that every older upload is in the DB.
    sync_cursor_video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    history_complete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settings: Mapped["ChannelSettings"] = relationship(back_populates="channel", uselist=False, cascade="all, delete-orphan")
    videos: Mapped[list["Video"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class ChannelSettings(Base):
    __tablename__ = "channel_settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), unique=True)
    preferred_languages: Mapped[list] = mapped_column(JSON, default=lambda: ["vi", "en", "original"])
    subtitle_preference: Mapped[str] = mapped_column(String(20), default="any") # manual, auto, any
    allow_translation: Mapped[bool] = mapped_column(Boolean, default=True)
    export_formats: Mapped[list] = mapped_column(JSON, default=lambda: ["srt", "txt"])
    sync_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    channel: Mapped[Channel] = relationship(back_populates="settings")


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("channel_id", "youtube_video_id", name="uq_channel_video"), Index("ix_videos_channel_published", "channel_id", "published_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    youtube_video_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(1024))
    url: Mapped[str] = mapped_column(String(2048))
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    video_type: Mapped[str] = mapped_column(String(20), default="video")
    status: Mapped[VideoStatus] = mapped_column(Enum(VideoStatus), default=VideoStatus.pending, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subtitle_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    channel: Mapped[Channel] = relationship(back_populates="videos")
    subtitles: Mapped[list["Subtitle"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class Subtitle(Base):
    __tablename__ = "subtitles"
    __table_args__ = (UniqueConstraint("video_id", "language_code", "format", name="uq_subtitle_file"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    language: Mapped[str] = mapped_column(String(128))
    language_code: Mapped[str] = mapped_column(String(32))
    is_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_translatable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_translated: Mapped[bool] = mapped_column(Boolean, default=False)
    format: Mapped[str] = mapped_column(String(8))
    file_path: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    video: Mapped[Video] = relationship(back_populates="subtitles")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "scheduled_at", "created_at"),
        # At most one active job per download target / per channel scan (partial unique indexes).
        Index("uq_active_job_video", "kind", "video_id", unique=True, sqlite_where=text(f"video_id IS NOT NULL AND {ACTIVE_JOB_SQL}")),
        Index("uq_active_job_channel", "kind", "channel_id", unique=True, sqlite_where=text(f"video_id IS NULL AND {ACTIVE_JOB_SQL}")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32)) # scan, download
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued, index=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id"), nullable=True, index=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id"), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_run_id: Mapped[str | None] = mapped_column(ForeignKey("sync_runs.id"), nullable=True, index=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)  # success/no_subtitle/language_unavailable/blocked/failed once terminal


class AppSetting(Base):
    """Key/value runtime settings shared by the API and worker processes."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class WorkerHeartbeat(Base):
    """One row per worker process; refreshed by that worker's heartbeat thread. Stale rows = dead workers."""
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    pid: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    state: Mapped[str] = mapped_column(String(16), default="idle")  # idle | busy
    current_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # informational, deliberately not an FK


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SyncRun(Base):
    """One channel sync = the scan job plus every download job it spawned (jobs.sync_run_id).

    Counters and status are a snapshot derived from those jobs by services/syncruns.refresh_sync_run_state.
    """
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_channel_status", "channel_id", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    mode: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    new_videos: Mapped[int] = mapped_column(Integer, default=0)
    queued_videos: Mapped[int] = mapped_column(Integer, default=0)
    successful: Mapped[int] = mapped_column(Integer, default=0)
    no_subtitle: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    language_unavailable: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default=SyncRunStatus.scanning.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
