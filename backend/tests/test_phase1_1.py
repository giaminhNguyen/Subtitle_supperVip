"""Phase 1.1: ownership races, timeouts vs lease/retry, foreign-key sanity."""
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import requests
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import worker
from app.config import Settings, settings
from app.models import Channel, ChannelSettings, Job, JobLog, JobStatus, Subtitle, SyncRun, Video, VideoStatus
from app.services import jobs as jobs_service
from app.services import queue, ratelimit, subtitles, youtube
from app.services.ratelimit import RateLimiter
from test_phase1 import (TIMEOUTS, FakeClock, FakeResponse, add_job, data_dir, get, new_session, no_wait,  # noqa: F401 (fixtures)
                         script_httpx, shared_db)

BACKEND = Path(__file__).resolve().parents[1]


def make_download_job(engine):
    s = new_session(engine)()
    channel = Channel(youtube_channel_id="UC1", title="K", url="u"); channel.settings = ChannelSettings(export_formats=["srt"])
    video = Video(channel=channel, youtube_video_id="abc", title="T", url="u", published_at=datetime(2025, 3, 1))
    s.add(video); s.commit()
    run = SyncRun(channel_id=channel.id, mode="new"); s.add(run); s.commit()
    job = Job(kind="download", channel_id=channel.id, video_id=video.id, payload={"sync_run_id": run.id}); s.add(job); s.commit()
    return s, job.id, video.id, run.id


def fake_transcript(monkeypatch):
    transcript = type("T", (), {"language": "Vietnamese", "language_code": "vi", "is_generated": False, "is_translatable": False})()
    monkeypatch.setattr(worker, "fetch_selected", lambda *a: (transcript, False, [{"text": "Chào", "start": 0, "duration": 1}]))


def worker_b_takes_over(engine):
    """A's lease expires, recovery requeues the job and worker B claims it (attempt 2)."""
    s = new_session(engine)()
    s.execute(text("UPDATE jobs SET lease_expires_at=:t"), {"t": datetime.utcnow() - timedelta(seconds=5)}); s.commit()
    assert queue.recover_stale_jobs(s) == 1
    claimed = queue.claim_next_job(s, "B", 300)
    assert claimed.worker_id == "B" and claimed.attempts == 2


def assert_untouched_by_a(s, job_id, video_id, run_id):
    s.expire_all(); job = s.get(Job, job_id)
    assert (job.status, job.worker_id, job.attempts, job.error, job.finished_at) == (JobStatus.processing, "B", 2, None, None)
    video = s.get(Video, video_id)
    assert video.status in (VideoStatus.pending, VideoStatus.processing) and video.retry_count == 0 and video.last_error is None
    run = s.get(SyncRun, run_id); assert (run.successful, run.failed, run.no_subtitle) == (0, 0, 0)
    assert not s.scalars(select(Subtitle)).all()
    messages = [x.message for x in s.scalars(select(JobLog))]
    assert not any(m == "Hoàn tất" or m.startswith(("Thất bại", "Lỗi tạm thời")) for m in messages), messages


# ---------- ownership races ----------
def test_finalize_race_lost_lease_between_file_write_and_db_commit(shared_db, data_dir, monkeypatch):
    s, job_id, video_id, run_id = make_download_job(shared_db); fake_transcript(monkeypatch)
    real_write = worker.atomic_write_text; stolen = []
    def write_then_lose_lease(path, content):  # the last step before the DB finalize
        real_write(path, content)
        if not stolen: stolen.append(1); worker_b_takes_over(shared_db)
    monkeypatch.setattr(worker, "atomic_write_text", write_then_lose_lease)
    assert worker.process_one()
    assert_untouched_by_a(s, job_id, video_id, run_id)


def test_stale_worker_late_exception_cannot_fail_or_requeue_new_owners_job(shared_db, monkeypatch):
    s, job_id, video_id, run_id = make_download_job(shared_db)
    def late_failure(*_):
        worker_b_takes_over(shared_db); raise ConnectionError("late network error")
    monkeypatch.setattr(worker, "process_download", late_failure)
    assert worker.process_one()
    assert_untouched_by_a(s, job_id, video_id, run_id)


@pytest.mark.parametrize("exc_type", [subtitles.SubtitleUnavailable, subtitles.LanguageUnavailable, subtitles.BlockedByYouTube])
def test_stale_worker_late_terminal_errors_are_discarded(shared_db, monkeypatch, exc_type):
    s, job_id, video_id, run_id = make_download_job(shared_db)
    def late(*_):
        worker_b_takes_over(shared_db); raise exc_type("x")
    monkeypatch.setattr(worker, "process_download", late)
    assert worker.process_one()
    assert_untouched_by_a(s, job_id, video_id, run_id)


def test_scan_stops_writing_once_ownership_is_lost(shared_db, monkeypatch):
    s = new_session(shared_db)()
    channel = Channel(youtube_channel_id="UC1", title="K", url="u"); channel.settings = ChannelSettings(); s.add(channel); s.commit()
    job = Job(kind="scan", channel_id=channel.id, payload={"mode": "new"}); s.add(job); s.commit(); job_id = job.id
    class FakeClient:
        def resolve_channel(self, _): return type("R", (), {"uploads_playlist_id": "UU"})()
        def list_uploads(self, _):
            for n in range(3):
                if n == 1: worker_b_takes_over(shared_db)  # lease lost while "fetching" page 2
                yield {"youtube_video_id": f"v{n}", "title": "t", "url": "u", "published_at": datetime(2025, 1, 1), "duration_seconds": 1, "thumbnail_url": None, "video_type": "video"}
    monkeypatch.setattr(jobs_service, "YouTubeDataClient", FakeClient)
    assert worker.process_one()
    s.expire_all()
    assert len(s.scalars(select(Video)).all()) == 1  # only what was committed while still the owner
    assert s.get(Job, job_id).worker_id == "B" and s.get(Job, job_id).status == JobStatus.processing


def test_success_finalizes_once_in_one_transaction(shared_db, data_dir, monkeypatch):
    s, job_id, video_id, run_id = make_download_job(shared_db); fake_transcript(monkeypatch)
    assert worker.process_one() and not worker.process_one()
    s.expire_all()
    assert s.get(Job, job_id).status == JobStatus.completed and s.get(Video, video_id).subtitle_path == "K/2025-03-T-abc/vi.srt"
    assert s.get(SyncRun, run_id).successful == 1 and len(s.scalars(select(Subtitle)).all()) == 1
    assert [x.message for x in s.scalars(select(JobLog))].count("Hoàn tất") == 1


def test_transient_failure_requeues_as_owner(shared_db, monkeypatch):
    s, job_id, video_id, run_id = make_download_job(shared_db)
    monkeypatch.setattr(worker, "process_download", lambda *_: (_ for _ in ()).throw(ConnectionError("net")))
    assert worker.process_one(); s.expire_all()
    job = s.get(Job, job_id)
    assert job.status == JobStatus.queued and job.worker_id is None and job.lease_expires_at is None and job.attempts == 1
    assert s.get(Video, video_id).retry_count == 1


# ---------- timeouts / retries / lease together ----------
def test_timeouts_then_success_keeps_lease_and_finalizes_once(shared_db, monkeypatch):
    clock = FakeClock(); limiter = RateLimiter(20, clock=clock.time, sleep=clock.sleep)
    monkeypatch.setattr(ratelimit, "_youtube_limiter", limiter); monkeypatch.setattr(ratelimit.time, "sleep", clock.sleep)
    script_httpx(monkeypatch, [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), FakeResponse(200, {"items": []})])
    s, job_id, *_ = make_download_job(shared_db); observer = new_session(shared_db)()
    seen = []
    def work(*_):
        seen.append(queue.recover_stale_jobs(observer))  # another worker scanning for stale jobs mid-retry
        assert youtube.YouTubeDataClient("K")._get("videos", {}) == {"items": []}
        seen.append(queue.recover_stale_jobs(observer))
    monkeypatch.setattr(worker, "process_download", work)
    assert worker.process_one() and seen == [0, 0]
    assert clock.now >= 6 and len(clock.sleeps) >= 4  # 3 requests spaced by the limiter + 2 backoffs
    s.expire_all(); assert s.get(Job, job_id).status == JobStatus.completed
    assert [x.message for x in s.scalars(select(JobLog))].count("Hoàn tất") == 1


def test_every_outbound_call_has_explicit_timeouts(monkeypatch, no_wait):
    TIMEOUTS.clear(); script_httpx(monkeypatch, [FakeResponse(200, {})]); get()
    timeout = TIMEOUTS[0]
    assert isinstance(timeout, httpx.Timeout) and None not in (timeout.connect, timeout.read, timeout.write, timeout.pool)
    captured = {}
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: captured.update(kw))
    subtitles._TimeoutSession().get("https://example.invalid")
    assert 0 < captured["timeout"][0] <= captured["timeout"][1] == settings.request_timeout_seconds
    subtitles._TimeoutSession().get("https://example.invalid", timeout=3); assert captured["timeout"] == 3
    assert isinstance(subtitles.transcript_api()._fetcher._http_client, subtitles._TimeoutSession)


def test_heartbeat_stops_renewing_after_max_runtime(shared_db):
    Session = new_session(shared_db); s = Session()
    job_id = add_job(s, status=JobStatus.processing, worker_id="A", lease_expires_at=datetime.utcnow() + timedelta(seconds=5))
    queue.Heartbeat(Session, job_id, "A", 3, max_runtime_seconds=0)._run()  # returns without renewing
    s.expire_all(); assert s.get(Job, job_id).lease_expires_at < datetime.utcnow() + timedelta(seconds=10)


def test_timing_settings_are_validated():
    with pytest.raises(ValueError): Settings(job_lease_seconds=5)
    with pytest.raises(ValueError): Settings(job_lease_seconds=60, job_max_runtime_seconds=60)
    with pytest.raises(ValueError): Settings(request_timeout_seconds=0)
    assert Settings().job_lease_seconds / 3 > 5  # heartbeat interval comfortably above SQLite busy_timeout


# ---------- foreign key sanity ----------
def fk_violations(engine):
    with engine.connect() as c: return c.execute(text("PRAGMA foreign_key_check")).fetchall()


def test_foreign_key_check_clean_after_normal_flow(shared_db, data_dir, monkeypatch):
    make_download_job(shared_db); fake_transcript(monkeypatch)
    assert worker.process_one() and fk_violations(shared_db) == []


def test_legacy_orphans_survive_migration_and_are_reported_not_deleted(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url); monkeypatch.setattr(settings, "data_dir", tmp_path)
    cfg = Config(str(BACKEND / "alembic.ini")); cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "0001")
    engine = create_engine(url)  # plain engine: FKs off, like the pre-Phase-1 app
    with engine.begin() as c:
        c.execute(text("INSERT INTO videos (id,channel_id,youtube_video_id,title,url,video_type,status,retry_count) VALUES ('orphan','missing','yt','t','u','video','pending',0)"))
        c.execute(text("INSERT INTO job_logs VALUES ('l1','missing-job','info','m','2026-01-01')"))
    command.upgrade(cfg, "head")
    assert sorted(row[0] for row in fk_violations(engine)) == ["job_logs", "videos"]
    with engine.connect() as c: assert c.execute(text("SELECT count(*) FROM videos")).scalar() == 1
    engine.dispose()
