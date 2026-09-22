"""Database-backed single-process worker. Run separately in production/Docker."""
import json, time, traceback
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import select
from .config import settings
from .database import SessionLocal
from .models import Channel, Job, JobStatus, Subtitle, SyncRun, Video, VideoStatus
from .services.jobs import due_channel_syncs, log, scan_channel
from .services.subtitles import BlockedByYouTube, LanguageUnavailable, SubtitleUnavailable, fetch_selected, serialize, video_folder


def process_download(db, job: Job):
    video = db.get(Video, job.video_id); channel = db.get(Channel, job.channel_id)
    if not video or not channel: raise RuntimeError("Video hoặc channel không tồn tại")
    options = channel.settings
    video.status, video.last_processed_at = VideoStatus.processing, datetime.utcnow(); db.flush()
    transcript, translated, snippets = fetch_selected(video.youtube_video_id, options.preferred_languages, options.subtitle_preference, options.allow_translation)
    folder = video_folder(channel.title, video); folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for fmt in options.export_formats:
        path = folder / f"{transcript.language_code}.{fmt}"; path.write_text(serialize(snippets, fmt), encoding="utf-8")
        previous = db.scalar(select(Subtitle).where(Subtitle.video_id == video.id, Subtitle.language_code == transcript.language_code, Subtitle.format == fmt))
        if not previous:
            db.add(Subtitle(video_id=video.id, language=transcript.language, language_code=transcript.language_code, is_generated=transcript.is_generated, is_translatable=transcript.is_translatable, is_translated=translated, format=fmt, file_path=str(path)))
        else: previous.file_path = str(path)
        paths.append(str(path))
    (folder / "metadata.json").write_text(json.dumps({"video_id": video.youtube_video_id, "title": video.title, "url": video.url, "published_at": video.published_at.isoformat() if video.published_at else None, "subtitle": {"language": transcript.language, "language_code": transcript.language_code, "generated": transcript.is_generated, "translated": translated}, "files": paths}, ensure_ascii=False, indent=2), encoding="utf-8")
    video.status, video.subtitle_path, video.last_error = VideoStatus.completed, paths[0] if paths else None, None
    if run_id := job.payload.get("sync_run_id"):
        run = db.get(SyncRun, run_id)
        if run: run.successful += 1


def process_one() -> bool:
    db = SessionLocal()
    try:
        due_channel_syncs(db); db.commit()
        job = db.scalar(select(Job).where(Job.status == JobStatus.queued, Job.scheduled_at <= datetime.utcnow()).order_by(Job.created_at).limit(1))
        if not job: return False
        job.status, job.started_at, job.attempts = JobStatus.processing, datetime.utcnow(), job.attempts + 1; log(db, job, f"Bắt đầu lần thử {job.attempts}"); db.commit()
        try:
            if job.kind == "scan": scan_channel(db, db.get(Channel, job.channel_id), job.payload.get("mode", "new"), job.payload.get("since"))
            elif job.kind == "download": process_download(db, job)
            else: raise RuntimeError(f"Loại job không hỗ trợ: {job.kind}")
            job.status, job.finished_at, job.error = JobStatus.completed, datetime.utcnow(), None; log(db, job, "Hoàn tất")
        except SubtitleUnavailable as exc:
            v = db.get(Video, job.video_id); v.status, v.last_error, v.last_processed_at = VideoStatus.no_subtitle, str(exc), datetime.utcnow(); job.status, job.finished_at, job.error = JobStatus.completed, datetime.utcnow(), str(exc); log(db, job, "Video không có subtitle")
            if run_id := job.payload.get("sync_run_id"):
                run = db.get(SyncRun, run_id)
                if run: run.no_subtitle += 1
        except LanguageUnavailable as exc:
            v = db.get(Video, job.video_id); v.status, v.last_error, v.last_processed_at = VideoStatus.language_unavailable, str(exc), datetime.utcnow(); job.status, job.finished_at, job.error = JobStatus.completed, datetime.utcnow(), str(exc); log(db, job, "Không có ngôn ngữ yêu cầu")
        except BlockedByYouTube as exc:
            v = db.get(Video, job.video_id); v.status, v.last_error = VideoStatus.blocked, str(exc); job.status, job.error, job.finished_at = JobStatus.failed, str(exc), datetime.utcnow(); log(db, job, "YouTube đã chặn request", "error")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"; v = db.get(Video, job.video_id) if job.video_id else None
            if v: v.retry_count, v.last_error, v.last_processed_at = v.retry_count + 1, error, datetime.utcnow()
            job.error = error
            if job.attempts < job.max_attempts:
                job.status, job.scheduled_at = JobStatus.queued, datetime.utcnow() + timedelta(seconds=min(300, 2 ** job.attempts * 5)); log(db, job, f"Lỗi tạm thời; thử lại: {error}", "warning")
                if v: v.status = VideoStatus.queued
            else:
                job.status, job.finished_at = JobStatus.failed, datetime.utcnow(); log(db, job, f"Thất bại: {error}", "error")
                if v: v.status = VideoStatus.failed
                if run_id := job.payload.get("sync_run_id"):
                    run = db.get(SyncRun, run_id)
                    if run: run.failed += 1
        db.commit(); return True
    finally: db.close()


def recover_interrupted():
    db = SessionLocal()
    try:
        jobs = db.scalars(select(Job).where(Job.status == JobStatus.processing)).all()
        for job in jobs: job.status, job.scheduled_at = JobStatus.queued, datetime.utcnow(); log(db, job, "Khôi phục job bị gián đoạn sau restart", "warning")
        db.commit()
    finally: db.close()


if __name__ == "__main__":
    recover_interrupted()
    while True:
        process_one(); time.sleep(settings.worker_poll_seconds)
