"""Phase 2: playlist caching, incremental sync, SyncRun lifecycle, idempotent counters, migration 0003."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import database, main, worker
from app.config import settings
from app.database import make_engine
from app.models import Channel, ChannelSettings, Job, JobLog, JobStatus, SyncRun, Video, VideoStatus
from app.services import jobs as jobs_service
from app.services import queue, subtitles
from app.services.syncruns import refresh_sync_run_state
from app.services.youtube import PlaylistNotFoundError
from test_phase1 import data_dir, new_session, shared_db  # noqa: F401 (fixtures)
from test_phase1_1 import fake_transcript, worker_b_takes_over

BACKEND = Path(__file__).resolve().parents[1]


# ---------- fakes / helpers ----------
def meta(video_id):
    return {"youtube_video_id": video_id, "title": video_id, "url": "u", "published_at": datetime(2025, 1, 1), "duration_seconds": 10, "thumbnail_url": None, "video_type": "video"}


class FakeYouTube:
    """Scripted uploads playlist, newest first. `unavailable` ids come back without metadata (private/deleted)."""
    def __init__(self, video_ids, playlist_id="PL", page_size=50, unavailable=(), on_page=None, fail_at_page=None):
        self.video_ids, self.playlist_id, self.page_size = list(video_ids), playlist_id, page_size
        self.unavailable, self.on_page, self.fail_at_page = set(unavailable), on_page, fail_at_page
        self.pages_fetched = 0; self.resolve_calls = []

    def resolve_channel(self, url):
        self.resolve_calls.append(url)
        return type("R", (), {"uploads_playlist_id": self.playlist_id})()

    def list_upload_pages(self, playlist_id):
        if playlist_id != self.playlist_id: raise PlaylistNotFoundError("gone")
        for start in range(0, len(self.video_ids), self.page_size):
            if self.fail_at_page is not None and self.pages_fetched == self.fail_at_page: raise ConnectionError("network down")
            self.pages_fetched += 1
            if self.on_page: self.on_page(self.pages_fetched)
            yield [{"youtube_video_id": v, "metadata": None if v in self.unavailable else meta(v)} for v in self.video_ids[start:start + self.page_size]]


NEW60 = [f"n{i}" for i in range(60)]


def install(monkeypatch, fake):
    monkeypatch.setattr(jobs_service, "YouTubeDataClient", lambda *a: fake)
    return fake


def make_channel(engine, playlist="PL", cursor=None, complete=False, known=()):
    s = new_session(engine)()
    channel = Channel(youtube_channel_id="UC1", title="K", url="https://www.youtube.com/@k", uploads_playlist_id=playlist,
                      sync_cursor_video_id=cursor, history_complete_at=datetime.utcnow() if complete else None)
    channel.settings = ChannelSettings(export_formats=["srt"]); s.add(channel); s.commit()
    if known:
        s.execute(insert(Video), [{"id": f"k-{v}", "channel_id": channel.id, "youtube_video_id": v, "title": v, "url": "u", "video_type": "video", "status": VideoStatus.completed, "retry_count": 0} for v in known]); s.commit()
    return s, channel.id


def scan(engine, channel_id, mode="new", **payload):
    s = new_session(engine)()
    job = jobs_service.enqueue(s, "scan", channel_id=channel_id, payload={"mode": mode, **payload}); s.commit(); job_id = job.id; s.close()
    assert worker.process_one()  # the scan job is the oldest queued job
    return job_id


def drain():
    while worker.process_one(): pass


def channel_state(s, channel_id):
    s.expire_all(); c = s.get(Channel, channel_id)
    return c.sync_cursor_video_id, c.history_complete_at is not None


def videos_of(s): return {v.youtube_video_id for v in s.scalars(select(Video))}


def only_run(s):
    s.expire_all(); return s.scalars(select(SyncRun)).one()


# ---------- playlist caching ----------
def test_new_channel_stores_uploads_playlist_id(shared_db, monkeypatch):
    resolved = type("R", (), {"channel_id": "UCnew", "title": "T", "avatar_url": None, "uploads_playlist_id": "UUnew"})()
    monkeypatch.setattr(main, "YouTubeDataClient", lambda: type("C", (), {"resolve_channel": lambda self, url: resolved})())
    assert TestClient(main.app).post("/api/channels", json={"url": "https://www.youtube.com/@new"}).status_code == 201
    with database.SessionLocal() as db: assert db.scalar(select(Channel.uploads_playlist_id)) == "UUnew"


def test_cached_playlist_is_not_resolved_again(shared_db, monkeypatch):
    fake = install(monkeypatch, FakeYouTube(["a", "b"])); s, channel_id = make_channel(shared_db)
    scan(shared_db, channel_id); assert fake.resolve_calls == [] and videos_of(s) == {"a", "b"}


def test_legacy_channel_without_playlist_resolves_once_and_persists(shared_db, monkeypatch):
    fake = install(monkeypatch, FakeYouTube(["a"], playlist_id="UUfound")); s, channel_id = make_channel(shared_db, playlist=None)
    scan(shared_db, channel_id); drain(); scan(shared_db, channel_id)
    assert len(fake.resolve_calls) == 1 and fake.resolve_calls[0].endswith("/channel/UC1")
    s.expire_all(); assert s.get(Channel, channel_id).uploads_playlist_id == "UUfound"


def test_stale_playlist_is_re_resolved_exactly_once(shared_db, monkeypatch):
    fake = install(monkeypatch, FakeYouTube(["a"], playlist_id="NEW")); s, channel_id = make_channel(shared_db, playlist="OLD")
    job_id = scan(shared_db, channel_id)
    assert len(fake.resolve_calls) == 1 and videos_of(s) == {"a"}
    s.expire_all(); assert s.get(Channel, channel_id).uploads_playlist_id == "NEW" and s.get(Job, job_id).status == JobStatus.completed


def test_still_invalid_after_re_resolve_does_not_loop(shared_db, monkeypatch):
    fake = install(monkeypatch, FakeYouTube(["a"], playlist_id="NEW"))
    fake.resolve_channel = lambda url: (fake.resolve_calls.append(url), type("R", (), {"uploads_playlist_id": "STILL-BAD"})())[1]
    s, channel_id = make_channel(shared_db, playlist="OLD")
    job_id = scan(shared_db, channel_id)
    assert len(fake.resolve_calls) == 1  # one re-resolve per attempt, never a loop
    s.expire_all(); job = s.get(Job, job_id); assert job.status == JobStatus.queued and "PlaylistNotFoundError" in job.error


# ---------- incremental scan ----------
def test_first_sync_is_a_full_scan_and_sets_state(shared_db, monkeypatch):
    fake = install(monkeypatch, FakeYouTube([f"v{i}" for i in range(120)])); s, channel_id = make_channel(shared_db)
    scan(shared_db, channel_id)
    assert fake.pages_fetched == 3 and len(videos_of(s)) == 120 and channel_state(s, channel_id) == ("v0", True)


def test_legacy_channel_with_videos_but_no_cursor_never_reports_zero_new(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(120)]; fake = install(monkeypatch, FakeYouTube(ids))
    s, channel_id = make_channel(shared_db, known=ids[60:])  # DB only has the older half (e.g. interrupted old scan)
    scan(shared_db, channel_id)
    assert fake.pages_fetched == 3 and videos_of(s) == set(ids)


def test_zero_new_stops_after_one_page(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(300)]; fake = install(monkeypatch, FakeYouTube(ids))
    s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=ids)
    scan(shared_db, channel_id)
    assert fake.pages_fetched == 1 and channel_state(s, channel_id) == ("v0", True)
    assert only_run(s).new_videos == 0


def test_one_new_video_advances_cursor(shared_db, monkeypatch):
    old = [f"v{i}" for i in range(300)]; fake = install(monkeypatch, FakeYouTube(["NEW"] + old))
    s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=old)
    scan(shared_db, channel_id)
    assert fake.pages_fetched == 1 and "NEW" in videos_of(s) and channel_state(s, channel_id) == ("NEW", True)
    assert only_run(s).new_videos == 1 and s.scalars(select(Job).where(Job.kind == "download")).one().video_id


def test_many_new_videos_across_page_boundary(shared_db, monkeypatch):
    old = [f"v{i}" for i in range(300)]; fresh = [f"n{i}" for i in range(60)]
    fake = install(monkeypatch, FakeYouTube(fresh + old)); s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=old)
    scan(shared_db, channel_id)
    assert set(fresh) <= videos_of(s) and only_run(s).new_videos == 60
    assert fake.pages_fetched == 2  # 60 new + cursor + 39 known: needs page 2, does not need page 3


def test_duplicate_entries_create_one_video_and_one_job(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["a", "b", "a", "c", "b"], page_size=2)); s, channel_id = make_channel(shared_db)
    scan(shared_db, channel_id)
    assert s.scalars(select(Video).where(Video.youtube_video_id == "a")).all().__len__() == 1 and len(videos_of(s)) == 3
    assert len(s.scalars(select(Job).where(Job.kind == "download")).all()) == 3


def test_missing_cursor_falls_back_to_deep_scan_and_recovers(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(200)]; fake = install(monkeypatch, FakeYouTube(["NEW"] + ids))  # old cursor "gone" was deleted
    s, channel_id = make_channel(shared_db, cursor="gone", complete=True, known=ids)
    scan(shared_db, channel_id)
    assert fake.pages_fetched == 5 and "NEW" in videos_of(s) and channel_state(s, channel_id) == ("NEW", True)
    assert "cursor not found" in s.scalars(select(JobLog.message).where(JobLog.message.like("Incremental%"))).one()


def test_full_scan_ignores_the_boundary(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(300)]; fake = install(monkeypatch, FakeYouTube(ids))
    s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=ids)
    scan(shared_db, channel_id, mode="all")
    assert fake.pages_fetched == 6


def test_private_entries_do_not_break_or_confuse_the_boundary(shared_db, monkeypatch):
    old = [f"v{i}" for i in range(100)]
    fake = install(monkeypatch, FakeYouTube(["NEW", "v0"] + old[1:], unavailable={"v0", "v5", "v6"}))  # the cursor itself went private
    s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=[v for v in old if v not in {"v0", "v5", "v6"}])
    scan(shared_db, channel_id)
    assert fake.pages_fetched == 1 and "NEW" in videos_of(s) and "v5" not in videos_of(s)


def test_since_scan_never_touches_sync_state(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["a", "b"])); s, channel_id = make_channel(shared_db, cursor="old", complete=False)
    scan(shared_db, channel_id, mode="since", since="2020-01-01T00:00:00")
    assert channel_state(s, channel_id) == ("old", False)


def test_failed_scan_does_not_advance_cursor(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(200)]; install(monkeypatch, FakeYouTube(NEW60 + ids, fail_at_page=1))  # 60 new: page 2 is needed
    s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=ids)
    job_id = scan(shared_db, channel_id)
    assert channel_state(s, channel_id) == ("v0", True)
    s.expire_all(); assert s.get(Job, job_id).status == JobStatus.queued  # will retry; page-1 work is kept, idempotent


def test_scan_retry_after_failure_reuses_run_and_finishes_correctly(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(200)]; broken = FakeYouTube(NEW60 + ids, fail_at_page=1)
    install(monkeypatch, broken); s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=ids)
    job_id = scan(shared_db, channel_id)
    s.execute(text("UPDATE jobs SET scheduled_at = :t"), {"t": datetime.utcnow() - timedelta(seconds=1)}); s.commit()
    install(monkeypatch, FakeYouTube(NEW60 + ids)); assert worker.process_one()
    assert len(s.scalars(select(SyncRun)).all()) == 1  # a restart/retry never creates a second run
    assert channel_state(s, channel_id) == ("n0", True) and only_run(s).new_videos == 60  # 50 counted by the failed attempt + 10 by the retry, not lost or repeated


def test_lost_ownership_mid_scan_leaves_cursor_run_and_jobs_alone(shared_db, monkeypatch):
    ids = [f"v{i}" for i in range(200)]
    fake = install(monkeypatch, FakeYouTube(NEW60 + ids, on_page=lambda n: worker_b_takes_over(shared_db) if n == 2 else None))
    s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=ids)
    job_id = scan(shared_db, channel_id)
    s.expire_all(); job = s.get(Job, job_id)
    assert channel_state(s, channel_id) == ("v0", True) and (job.worker_id, job.status) == ("B", JobStatus.processing)
    assert only_run(s).status == "scanning" and only_run(s).finished_at is None
    assert only_run(s).new_videos == 50 and len(videos_of(s)) == 250  # page 1 was committed while still owner; page 2 was not written


def test_large_history_needs_few_requests(shared_db, monkeypatch):
    known = [f"v{i}" for i in range(5000)]; new = ["n1", "n2", "n3"]
    fake = install(monkeypatch, FakeYouTube(new + known)); s, channel_id = make_channel(shared_db, cursor="v0", complete=True, known=known)
    scan(shared_db, channel_id)
    incremental_pages = fake.pages_fetched
    drain(); fake.pages_fetched = 0
    make_channel_full = scan(shared_db, channel_id, mode="all")
    assert incremental_pages == 1 and fake.pages_fetched == 101 and only_run_count(s) == 2


def only_run_count(s): s.expire_all(); return len(s.scalars(select(SyncRun)).all())


# ---------- SyncRun lifecycle ----------
def outcome_fetch(monkeypatch, plan):
    transcript = type("T", (), {"language": "Vietnamese", "language_code": "vi", "is_generated": False, "is_translatable": False})()
    def fetch(video_id, *a):
        action = plan.get(video_id, "ok")
        if isinstance(action, Exception): raise action
        return transcript, False, [{"text": "x", "start": 0, "duration": 1}]
    monkeypatch.setattr(worker, "fetch_selected", fetch)


def test_run_stays_open_until_every_child_job_is_terminal(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1", "v2", "v3", "v4"])); s, channel_id = make_channel(shared_db)
    outcome_fetch(monkeypatch, {"v2": subtitles.SubtitleUnavailable("no"), "v3": subtitles.LanguageUnavailable("lang"), "v4": subtitles.BlockedByYouTube("blocked")})
    scan(shared_db, channel_id)
    run = only_run(s)
    assert (run.status, run.finished_at, run.queued_videos, run.new_videos) == ("downloading", None, 4, 4)  # scan done, children queued
    assert worker.process_one(); run = only_run(s)
    assert run.status == "downloading" and run.finished_at is None and run.successful == 1
    drain(); run = only_run(s)
    assert (run.successful, run.no_subtitle, run.language_unavailable, run.failed) == (1, 1, 1, 1)
    assert run.status == "partial" and run.finished_at is not None


def test_all_success_completes_and_all_failed_fails(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1", "v2"])); s, channel_id = make_channel(shared_db)
    outcome_fetch(monkeypatch, {}); scan(shared_db, channel_id); drain()
    assert only_run(s).status == "completed"
    s.execute(text("DELETE FROM job_logs")); s.execute(text("DELETE FROM subtitles")); s.execute(text("DELETE FROM jobs")); s.execute(text("DELETE FROM sync_runs")); s.execute(text("DELETE FROM videos")); s.commit()
    outcome_fetch(monkeypatch, {"v1": subtitles.BlockedByYouTube("b"), "v2": subtitles.BlockedByYouTube("b")})
    s.execute(text("UPDATE channels SET sync_cursor_video_id=NULL, history_complete_at=NULL")); s.commit()
    scan(shared_db, channel_id); drain()
    assert only_run(s).status == "failed" and only_run(s).finished_at is not None


def test_run_is_scanning_and_unfinished_while_the_scan_is_active(shared_db, monkeypatch):
    seen = []
    def peek(page):
        with database.SessionLocal() as other: run = other.scalars(select(SyncRun)).one(); seen.append((run.status, run.finished_at))
    install(monkeypatch, FakeYouTube([f"v{i}" for i in range(120)], on_page=peek)); s, channel_id = make_channel(shared_db)
    scan(shared_db, channel_id)
    assert seen == [("scanning", None)] * 3


def test_failed_scan_run_reflects_failure_after_retries_are_exhausted(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["a"], fail_at_page=0)); s, channel_id = make_channel(shared_db)
    job_id = scan(shared_db, channel_id)
    s.execute(text("UPDATE jobs SET attempts = max_attempts - 1")); s.commit()
    s.execute(text("UPDATE jobs SET scheduled_at = :t"), {"t": datetime.utcnow() - timedelta(seconds=1)}); s.commit()
    assert worker.process_one()
    run = only_run(s); assert run.status == "failed" and run.finished_at is not None and "ConnectionError" in run.error


def test_temporary_failure_then_success_counts_success_only(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1"])); s, channel_id = make_channel(shared_db); scan(shared_db, channel_id)
    outcome_fetch(monkeypatch, {"v1": ConnectionError("blip")}); assert worker.process_one()
    run = only_run(s); assert (run.failed, run.successful, run.status, run.finished_at) == (0, 0, "downloading", None)
    outcome_fetch(monkeypatch, {}); s.execute(text("UPDATE jobs SET scheduled_at = :t"), {"t": datetime.utcnow() - timedelta(seconds=1)}); s.commit()
    assert worker.process_one()
    run = only_run(s); assert (run.failed, run.successful, run.status) == (0, 1, "completed")


def test_refresh_is_idempotent_and_sets_finished_at_once(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1"])); s, channel_id = make_channel(shared_db); outcome_fetch(monkeypatch, {}); scan(shared_db, channel_id); drain()
    first = only_run(s); snapshot = (first.successful, first.status, first.finished_at, first.queued_videos)
    for _ in range(3):
        with database.SessionLocal() as db: refresh_sync_run_state(db, first.id, now=datetime.utcnow() + timedelta(days=1)); db.commit()
    again = only_run(s); assert (again.successful, again.status, again.finished_at, again.queued_videos) == snapshot


def test_restart_recovers_child_job_without_double_counting(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1", "v2"])); s, channel_id = make_channel(shared_db); outcome_fetch(monkeypatch, {}); scan(shared_db, channel_id)
    # a worker claims a child, then dies; its lease lapses
    with database.SessionLocal() as db: claimed = queue.claim_next_job(db, "dead-worker", 60)
    s.execute(text("UPDATE jobs SET lease_expires_at = :t WHERE id = :id"), {"t": datetime.utcnow() - timedelta(seconds=1), "id": claimed.id}); s.commit()
    drain(); run = only_run(s)
    assert (run.successful, run.failed, run.queued_videos, run.status) == (2, 0, 2, "completed") and len(s.scalars(select(SyncRun)).all()) == 1


def test_exhausted_stale_child_fails_and_closes_the_run(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1"])); s, channel_id = make_channel(shared_db); scan(shared_db, channel_id)
    with database.SessionLocal() as db: claimed = queue.claim_next_job(db, "dead-worker", 60)
    s.execute(text("UPDATE jobs SET lease_expires_at = :t, attempts = max_attempts WHERE id = :id"), {"t": datetime.utcnow() - timedelta(seconds=1), "id": claimed.id}); s.commit()
    with database.SessionLocal() as db: queue.recover_stale_jobs(db)
    run = only_run(s); assert (run.status, run.failed) == ("failed", 1) and run.finished_at is not None


def test_retrying_a_failed_child_reopens_then_closes_the_run(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1"])); s, channel_id = make_channel(shared_db)
    outcome_fetch(monkeypatch, {"v1": subtitles.BlockedByYouTube("b")}); scan(shared_db, channel_id); drain()
    assert only_run(s).status == "failed" and only_run(s).finished_at is not None
    failed_job = s.scalars(select(Job).where(Job.kind == "download")).one()
    client = TestClient(main.app); assert client.post(f"/api/jobs/{failed_job.id}/retry").status_code == 200
    run = only_run(s); assert (run.status, run.finished_at, run.failed) == ("downloading", None, 0)
    outcome_fetch(monkeypatch, {}); drain(); run = only_run(s)
    assert (run.status, run.successful, run.failed) == ("completed", 1, 0) and run.finished_at is not None


def test_cancelled_child_makes_run_partial_not_stuck(shared_db, data_dir, monkeypatch):
    install(monkeypatch, FakeYouTube(["v1", "v2"])); s, channel_id = make_channel(shared_db); outcome_fetch(monkeypatch, {}); scan(shared_db, channel_id)
    victim = s.scalars(select(Job).where(Job.kind == "download").order_by(Job.created_at)).first()
    assert TestClient(main.app).post(f"/api/jobs/{victim.id}/cancel").status_code == 200
    drain(); run = only_run(s); assert (run.status, run.successful) == ("partial", 1) and run.finished_at is not None


# ---------- job idempotency (DB level) ----------
def test_one_active_job_per_target_is_a_db_guarantee(shared_db):
    s, channel_id = make_channel(shared_db)
    video = Video(channel_id=channel_id, youtube_video_id="x", title="x", url="u"); s.add(video); s.commit()
    first = jobs_service.enqueue(s, "download", channel_id=channel_id, video_id=video.id); s.commit()
    assert jobs_service.enqueue(s, "download", channel_id=channel_id, video_id=video.id).id == first.id
    with pytest.raises(IntegrityError):
        s.execute(insert(Job).values(id="dup", kind="download", status=JobStatus.queued, video_id=video.id, payload={}, attempts=0, max_attempts=5, scheduled_at=datetime.utcnow(), created_at=datetime.utcnow()))
    s.rollback()
    with pytest.raises(IntegrityError):
        s.execute(insert(Job).values(id="dup2", kind="scan", status=JobStatus.queued, channel_id=channel_id, payload={}, attempts=0, max_attempts=5, scheduled_at=datetime.utcnow(), created_at=datetime.utcnow()))
        s.execute(insert(Job).values(id="dup3", kind="scan", status=JobStatus.processing, channel_id=channel_id, payload={}, attempts=0, max_attempts=5, scheduled_at=datetime.utcnow(), created_at=datetime.utcnow()))
    s.rollback()


def test_finished_or_failed_jobs_do_not_block_a_new_one(shared_db):
    s, channel_id = make_channel(shared_db)
    video = Video(channel_id=channel_id, youtube_video_id="x", title="x", url="u"); s.add(video); s.commit()
    first = jobs_service.enqueue(s, "download", channel_id=channel_id, video_id=video.id); first.status = JobStatus.failed; s.commit()
    assert jobs_service.enqueue(s, "download", channel_id=channel_id, video_id=video.id).id != first.id


def test_enqueue_adopts_unowned_active_job_into_the_run(shared_db):
    s, channel_id = make_channel(shared_db)
    video = Video(channel_id=channel_id, youtube_video_id="x", title="x", url="u"); s.add(video)
    run = SyncRun(channel_id=channel_id, mode="new"); s.add(run); s.commit()
    manual = jobs_service.enqueue(s, "download", channel_id=channel_id, video_id=video.id); s.commit()
    assert jobs_service.enqueue(s, "download", channel_id=channel_id, video_id=video.id, sync_run_id=run.id).sync_run_id == run.id and manual.id


# ---------- migration 0003 ----------
def alembic_config(tmp_path, monkeypatch, name):
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    monkeypatch.setattr(settings, "database_url", url); monkeypatch.setattr(settings, "data_dir", tmp_path)
    cfg = Config(str(BACKEND / "alembic.ini")); cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg, url


def test_fresh_database_upgrades_to_head_with_indexes(tmp_path, monkeypatch):
    cfg, url = alembic_config(tmp_path, monkeypatch, "fresh.db"); command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as c:
        jobs_indexes = {row[1] for row in c.execute(text("PRAGMA index_list(jobs)"))}
        assert {"ix_jobs_claim", "ix_jobs_sync_run_id", "uq_active_job_video", "uq_active_job_channel"} <= jobs_indexes
        assert "ix_videos_channel_published" in {row[1] for row in c.execute(text("PRAGMA index_list(videos)"))}
        assert {"uploads_playlist_id", "sync_cursor_video_id", "history_complete_at"} <= {row[1] for row in c.execute(text("PRAGMA table_info(channels)"))}
    engine.dispose()


def seed_0002(url, duplicate_jobs=False):
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO channels VALUES ('c1','UC1','https://www.youtube.com/@k','Kenh',NULL,'2026-01-01',NULL,NULL,2)"))
        c.execute(text("INSERT INTO channel_settings VALUES ('cs1','c1','[\"vi\"]','any',1,'[\"srt\"]',24)"))
        c.execute(text("INSERT INTO videos (id,channel_id,youtube_video_id,title,url,video_type,status,retry_count,subtitle_path) VALUES ('v1','c1','yt1','t1','u','video','completed',0,'a/b.srt'), ('v2','c1','yt2','t2','u','video','no_subtitle',0,NULL), ('v3','c1','yt3','t3','u','video','blocked',0,NULL)"))
        c.execute(text("INSERT INTO subtitles VALUES ('s1','v1','Vietnamese','vi',0,0,0,'srt','a/b.srt','2026-01-01')"))
        c.execute(text("INSERT INTO sync_runs VALUES ('r1','c1','new','2026-01-01','2026-01-01',3,3,1,1,1)"))
        c.execute(text("INSERT INTO jobs (id,kind,status,channel_id,video_id,payload,attempts,max_attempts,scheduled_at,created_at,error) VALUES ('j1','download','completed','c1','v1','{\"sync_run_id\": \"r1\"}',1,5,'2026-01-01','2026-01-01',NULL), ('j2','download','completed','c1','v2','{\"sync_run_id\": \"r1\"}',1,5,'2026-01-01','2026-01-01','no sub'), ('j3','download','failed','c1','v3','{\"sync_run_id\": \"r1\"}',5,5,'2026-01-01','2026-01-01','blocked')"))
        if duplicate_jobs:
            c.execute(text("INSERT INTO jobs (id,kind,status,channel_id,video_id,payload,attempts,max_attempts,scheduled_at,created_at) VALUES ('d1','download','queued','c1','v3','{}',0,5,'2026-01-01','2026-01-01'), ('d2','download','queued','c1','v3','{}',0,5,'2026-01-01','2026-01-01')"))
        c.execute(text("INSERT INTO app_settings VALUES ('youtube_api_key','KEY1234567890','2026-01-01')"))
    return engine


def test_0002_database_upgrades_keeping_data_and_backfilling_links(tmp_path, monkeypatch):
    cfg, url = alembic_config(tmp_path, monkeypatch, "old.db"); command.upgrade(cfg, "0002"); engine = seed_0002(url)
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM videos")).scalar() == 3 and c.execute(text("SELECT count(*) FROM subtitles")).scalar() == 1
        assert c.execute(text("SELECT value FROM app_settings")).scalar() == "KEY1234567890"
        assert dict(c.execute(text("SELECT id, sync_run_id FROM jobs")).fetchall()) == {"j1": "r1", "j2": "r1", "j3": "r1"}
        assert dict(c.execute(text("SELECT id, outcome FROM jobs")).fetchall()) == {"j1": "success", "j2": "no_subtitle", "j3": "blocked"}
        assert c.execute(text("SELECT status FROM sync_runs")).scalar() == "completed"
        assert c.execute(text("SELECT uploads_playlist_id, sync_cursor_video_id, history_complete_at FROM channels")).one() == (None, None, None)
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()


def test_migration_keeps_legacy_duplicate_active_jobs_and_skips_only_that_index(tmp_path, monkeypatch):
    cfg, url = alembic_config(tmp_path, monkeypatch, "dups.db"); command.upgrade(cfg, "0002"); engine = seed_0002(url, duplicate_jobs=True)
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM jobs WHERE id IN ('d1','d2')")).scalar() == 2  # nothing deleted
        names = {row[1] for row in c.execute(text("PRAGMA index_list(jobs)"))}
        assert "uq_active_job_video" not in names and "uq_active_job_channel" in names
    engine.dispose()


def test_migrated_legacy_channel_still_syncs(tmp_path, monkeypatch):
    cfg, url = alembic_config(tmp_path, monkeypatch, "legacy.db"); command.upgrade(cfg, "0002"); seed_0002(url).dispose(); command.upgrade(cfg, "head")
    engine = make_engine(url); monkeypatch.setitem(database.SessionLocal.kw, "bind", engine)
    fake = install(monkeypatch, FakeYouTube(["yt1", "yt2", "yt3", "yt4"], playlist_id="UUlegacy"))
    scan(engine, "c1")
    s = new_session(engine)()
    assert len(fake.resolve_calls) == 1 and "yt4" in videos_of(s) and channel_state(s, "c1") == ("yt1", True)
    engine.dispose()
