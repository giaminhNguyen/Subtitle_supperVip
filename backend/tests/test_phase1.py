import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from app import config, database, worker
from app.config import settings
from app.models import AppSetting, Job, JobStatus
from app.services import queue, runtime_settings, storage, youtube
from app.services.ratelimit import RateLimiter, call_with_retry

BACKEND = Path(__file__).resolve().parents[1]


# ---------- fixtures ----------
def new_session(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


# ---------- rate limiter ----------
class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_enforces_requests_per_minute():
    clock = FakeClock()
    limiter = RateLimiter(20, clock=clock.time, sleep=clock.sleep)
    for _ in range(21):
        limiter.acquire()
    assert clock.now == pytest.approx(60.0)  # first request immediate, then one every 3s


def test_rate_limiter_zero_disables():
    clock = FakeClock()
    limiter = RateLimiter(0, clock=clock.time, sleep=clock.sleep)
    for _ in range(50):
        limiter.acquire()
    assert clock.sleeps == []


def test_retries_also_count_against_rate_limit():
    clock = FakeClock()
    limiter = RateLimiter(20, clock=clock.time, sleep=clock.sleep)
    attempts = []

    def flaky():
        attempts.append(clock.now)
        if len(attempts) < 3:
            raise ConnectionError("x")

    call_with_retry(flaky, category="t", is_retryable=lambda e: (True, None), limiter=limiter, sleep=clock.sleep)
    assert attempts[1] - attempts[0] >= 3 and attempts[2] - attempts[1] >= 3


# ---------- retry ----------
class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.text = str(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


TIMEOUTS = []


def script_httpx(monkeypatch, responses):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params)
        TIMEOUTS.append(timeout)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(youtube.httpx, "get", fake_get)
    return calls


def get(body_key="SECRETKEY123"):
    return youtube.YouTubeDataClient(body_key)._get("videos", {})


def test_retries_429_then_succeeds(monkeypatch, no_wait):
    calls = script_httpx(monkeypatch, [FakeResponse(429), FakeResponse(200, {"items": []})])
    assert get() == {"items": []}
    assert len(calls) == 2 and len(no_wait) == 1


def test_retries_transient_5xx_and_timeouts(monkeypatch, no_wait):
    calls = script_httpx(monkeypatch, [FakeResponse(503), httpx.ReadTimeout("t"), FakeResponse(200, {"ok": 1})])
    assert get() == {"ok": 1}
    assert len(calls) == 3


def test_retry_is_bounded_with_growing_backoff(monkeypatch, no_wait):
    calls = script_httpx(monkeypatch, [FakeResponse(500)])
    with pytest.raises(youtube.YouTubeError):
        get()
    assert len(calls) == settings.request_max_attempts
    assert no_wait == sorted(no_wait) and no_wait[1] > no_wait[0]


@pytest.mark.parametrize("status", [400, 401, 404])
def test_permanent_errors_not_retried(monkeypatch, no_wait, status):
    calls = script_httpx(monkeypatch, [FakeResponse(status)])
    with pytest.raises(youtube.YouTubeError):
        get()
    assert len(calls) == 1 and no_wait == []


def test_quota_403_not_retried_but_rate_limit_403_is(monkeypatch, no_wait):
    quota = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    limited = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
    calls = script_httpx(monkeypatch, [FakeResponse(403, quota)])
    with pytest.raises(youtube.YouTubeError):
        get()
    assert len(calls) == 1
    calls = script_httpx(monkeypatch, [FakeResponse(403, limited), FakeResponse(200, {})])
    get()
    assert len(calls) == 2


def test_api_key_never_leaks_in_errors(monkeypatch, no_wait):
    script_httpx(monkeypatch, [httpx.ConnectError("boom https://x/?key=SECRETKEY123")])
    with pytest.raises(youtube.YouTubeError) as caught:
        get()
    assert "SECRETKEY123" not in str(caught.value)
    script_httpx(monkeypatch, [FakeResponse(400, {"message": "bad key SECRETKEY123"})])
    with pytest.raises(youtube.YouTubeError) as caught:
        get()
    assert "SECRETKEY123" not in str(caught.value)


# ---------- worker loop ----------
def test_worker_does_not_sleep_while_queue_has_jobs():
    results = iter([True, True, True, False, False])
    sleeps = []
    calls = []

    def process():
        calls.append(1)
        return next(results)

    worker.worker_loop(process=process, sleep=sleeps.append, poll_seconds=2, should_stop=lambda: len(calls) >= 5)
    assert sleeps == [2, 2]


def test_worker_survives_unexpected_error():
    calls = []
    sleeps = []

    def process():
        calls.append(1)
        raise RuntimeError("db locked")

    worker.worker_loop(process=process, sleep=sleeps.append, poll_seconds=1, should_stop=lambda: len(calls) >= 2)
    assert sleeps == [1, 1]


# ---------- sqlite ----------
def test_sqlite_pragmas(shared_db):
    with shared_db.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() >= 1000
        assert connection.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"


# ---------- queue ----------
def add_job(session, **kwargs):
    job = Job(kind="download", **kwargs)
    session.add(job)
    session.commit()
    return job.id


def test_only_one_worker_claims_a_job(shared_db):
    Session = new_session(shared_db)
    s = Session()
    add_job(s)
    s.close()
    barrier = threading.Barrier(6)
    winners = []

    def attempt(name):
        session = Session()
        barrier.wait()
        try:
            if queue.claim_next_job(session, name, 60):
                winners.append(name)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(f"w{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1
    job = Session().scalars(select(Job)).one()
    assert job.status == JobStatus.processing and job.worker_id == winners[0] and job.attempts == 1 and job.lease_expires_at > datetime.utcnow()


def test_claim_skips_future_and_paused_jobs(shared_db):
    s = new_session(shared_db)()
    add_job(s, scheduled_at=datetime.utcnow() + timedelta(hours=1))
    add_job(s, status=JobStatus.paused)
    assert queue.claim_next_job(s, "w", 60) is None


def test_live_lease_is_not_recovered_but_stale_is(shared_db):
    s = new_session(shared_db)()
    live = add_job(s, status=JobStatus.processing, worker_id="A", lease_expires_at=datetime.utcnow() + timedelta(seconds=120), attempts=1)
    stale = add_job(s, status=JobStatus.processing, worker_id="B", lease_expires_at=datetime.utcnow() - timedelta(seconds=1), attempts=1)
    legacy = add_job(s, status=JobStatus.processing, attempts=1)  # processing row from a pre-lease install
    assert queue.recover_stale_jobs(s) == 2
    s.expire_all()
    assert s.get(Job, live).status == JobStatus.processing and s.get(Job, live).worker_id == "A"
    for job_id in (stale, legacy):
        assert s.get(Job, job_id).status == JobStatus.queued and s.get(Job, job_id).worker_id is None


def test_stale_job_out_of_attempts_fails_instead_of_looping(shared_db):
    s = new_session(shared_db)()
    job_id = add_job(s, status=JobStatus.processing, lease_expires_at=datetime.utcnow() - timedelta(seconds=1), attempts=5, max_attempts=5)
    queue.recover_stale_jobs(s)
    s.expire_all()
    assert s.get(Job, job_id).status == JobStatus.failed


def test_heartbeat_extends_lease_and_detects_lost_job(shared_db):
    Session = new_session(shared_db)
    s = Session()
    job_id = add_job(s, status=JobStatus.processing, worker_id="A", lease_expires_at=datetime.utcnow() + timedelta(seconds=1))
    assert queue.Heartbeat(Session, job_id, "A", 300).beat()
    s.expire_all()
    assert s.get(Job, job_id).lease_expires_at > datetime.utcnow() + timedelta(seconds=200)
    assert not queue.Heartbeat(Session, job_id, "B", 300).beat()
    queue.guard_job(s, job_id, "A", 0)
    s.rollback()
    with pytest.raises(queue.LostOwnership):
        queue.guard_job(s, job_id, "B", 0)
    with pytest.raises(queue.LostOwnership):
        queue.guard_job(s, job_id, "A", 1)  # right worker, wrong attempt


def test_worker_abandons_result_when_lease_was_taken(shared_db, monkeypatch):
    s = new_session(shared_db)()
    job_id = add_job(s)

    def steal(*_):
        thief = new_session(shared_db)()
        thief.execute(text("UPDATE jobs SET worker_id='other'"))
        thief.commit()
        thief.close()

    monkeypatch.setattr(worker, "process_download", steal)
    assert worker.process_one()
    s.expire_all()
    job = s.get(Job, job_id)
    assert job.status == JobStatus.processing and job.worker_id == "other"  # our completion was discarded


def test_process_one_completes_job_and_releases_lease(shared_db, monkeypatch):
    s = new_session(shared_db)()
    job_id = add_job(s)
    monkeypatch.setattr(worker, "process_download", lambda *_: None)
    assert worker.process_one() and not worker.process_one()
    s.expire_all()
    job = s.get(Job, job_id)
    assert job.status == JobStatus.completed and job.worker_id is None and job.lease_expires_at is None


# ---------- runtime API key ----------
def test_legacy_key_imported_into_db(shared_db, monkeypatch):
    monkeypatch.setattr(runtime_settings, "read_legacy_youtube_api_key", lambda: "LEGACYKEY12345")
    assert runtime_settings.get_youtube_api_key() == "LEGACYKEY12345"
    monkeypatch.setattr(runtime_settings, "read_legacy_youtube_api_key", lambda: "")
    assert runtime_settings.get_youtube_api_key() == "LEGACYKEY12345"  # now served from the DB
    with database.SessionLocal() as db:
        assert db.get(AppSetting, "youtube_api_key").value == "LEGACYKEY12345"


def test_db_key_overrides_legacy(shared_db, monkeypatch):
    monkeypatch.setattr(runtime_settings, "read_legacy_youtube_api_key", lambda: "LEGACYKEY12345")
    runtime_settings.set_youtube_api_key("DBKEY123456789")
    assert runtime_settings.get_youtube_api_key() == "DBKEY123456789"


def test_legacy_reader_skips_placeholder(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("YOUTUBE_API_KEY=replace_me\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(settings, "youtube_api_key", "")
    assert config.read_legacy_youtube_api_key() == ""
    env.write_text('YOUTUBE_API_KEY="realkey1234567"\n', encoding="utf-8")
    assert config.read_legacy_youtube_api_key() == "realkey1234567"


def test_api_writes_key_worker_reads_it_and_status_hides_it(shared_db, monkeypatch):
    from app.main import app

    monkeypatch.setattr(runtime_settings, "read_legacy_youtube_api_key", lambda: "")
    client = TestClient(app)
    assert client.get("/api/config/youtube").json() == {"configured": False}
    response = client.put("/api/config/youtube", json={"api_key": "AIzaSyTESTKEY12345"})
    assert response.json() == {"configured": True} and "AIzaSyTESTKEY12345" not in response.text
    assert "AIzaSyTESTKEY12345" not in client.get("/api/config/youtube").text
    assert youtube.YouTubeDataClient().key == "AIzaSyTESTKEY12345"  # what the worker's client uses
    assert client.put("/api/config/youtube", json={"api_key": "short\nkey12345"}).status_code == 422


# ---------- paths ----------
def test_relative_path_roundtrip_and_relocation(tmp_path, monkeypatch, data_dir):
    file = data_dir / "chan" / "vid" / "vi.srt"
    file.parent.mkdir(parents=True)
    file.write_text("x")
    stored = storage.to_stored_path(file)
    assert stored == "chan/vid/vi.srt"
    assert storage.resolve_subtitle_path(stored) == file.resolve()
    moved = tmp_path / "B" / "data"
    (moved / "chan" / "vid").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", moved)
    assert storage.resolve_subtitle_path(stored) == (moved / "chan" / "vid" / "vi.srt").resolve()


def test_legacy_absolute_paths(tmp_path, data_dir):
    inside = data_dir / "c" / "v.srt"
    inside.parent.mkdir()
    inside.write_text("x")
    assert storage.resolve_subtitle_path(str(inside)) == inside.resolve()
    outside = tmp_path / "elsewhere.srt"
    outside.write_text("x")
    assert storage.resolve_subtitle_path(str(outside)) == outside.resolve()  # legacy file still readable
    with pytest.raises(storage.UnsafePathError):
        storage.resolve_subtitle_path(str(tmp_path / "gone.srt"))
    with pytest.raises(storage.UnsafePathError):
        storage.to_stored_path(outside)


@pytest.mark.parametrize("bad", ["../../secret.txt", "chan/../../secret.txt"])
def test_path_traversal_rejected(tmp_path, data_dir, bad):
    (tmp_path / "secret.txt").write_text("secret")
    with pytest.raises(storage.UnsafePathError):
        storage.resolve_subtitle_path(bad)


# ---------- atomic write ----------
def test_atomic_write_success_leaves_no_temp(data_dir):
    target = data_dir / "c" / "v.srt"
    storage.atomic_write_text(target, "xin chào")
    assert target.read_text(encoding="utf-8") == "xin chào" and [p.name for p in target.parent.iterdir()] == ["v.srt"]


def test_atomic_write_failure_keeps_old_file(data_dir, monkeypatch):
    target = data_dir / "v.srt"
    target.write_text("old", encoding="utf-8")

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", boom)
    with pytest.raises(OSError):
        storage.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old" and [p.name for p in data_dir.iterdir()] == ["v.srt"]


# ---------- migration ----------
def test_upgrade_old_database_keeps_data_and_relativises_paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "data_dir", data)
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    inside, outside = data / "c" / "v" / "vi.srt", tmp_path.parent / "other" / "x.srt"
    with engine.begin() as c:
        c.execute(text("INSERT INTO channels VALUES ('c1','UC1','u','T',NULL,'2026-01-01',NULL,NULL,0)"))
        c.execute(
            text(
                "INSERT INTO videos (id,channel_id,youtube_video_id,title,url,video_type,status,retry_count,subtitle_path) VALUES ('v1','c1','yt','t','u','video','completed',0,:p)"
            ),
            {"p": str(inside)},
        )
        c.execute(text("INSERT INTO subtitles VALUES ('s1','v1','Vietnamese','vi',0,0,0,'srt',:p,'2026-01-01')"), {"p": str(inside)})
        c.execute(text("INSERT INTO subtitles VALUES ('s2','v1','English','en',0,0,0,'srt',:p,'2026-01-01')"), {"p": str(outside)})
        c.execute(
            text("INSERT INTO jobs (id,kind,status,payload,attempts,max_attempts,scheduled_at,created_at) VALUES ('j1','download','processing','{}',1,5,'2026-01-01','2026-01-01')")
        )
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT subtitle_path FROM videos")).scalar() == "c/v/vi.srt"
        assert dict(c.execute(text("SELECT id, file_path FROM subtitles")).fetchall()) == {"s1": "c/v/vi.srt", "s2": str(outside)}
        assert tuple(c.execute(text("SELECT status, worker_id FROM jobs")).one()) == ("processing", None)
        assert c.execute(text("SELECT count(*) FROM app_settings")).scalar() == 0
    engine.dispose()


# ---------- download stores portable paths ----------
def test_process_download_writes_atomically_and_stores_relative_paths(shared_db, data_dir, monkeypatch):
    from app.models import Channel, ChannelSettings, Subtitle, Video

    s = new_session(shared_db)()
    channel = Channel(youtube_channel_id="UC1", title="Kênh", url="u")
    channel.settings = ChannelSettings(export_formats=["srt", "txt"])
    video = Video(channel=channel, youtube_video_id="abc", title="Tiêu đề", url="u", published_at=datetime(2025, 3, 1))
    s.add(video)
    s.commit()
    job = Job(kind="download", channel_id=channel.id, video_id=video.id, status=JobStatus.processing, worker_id=worker.WORKER_ID)
    s.add(job)
    s.commit()
    transcript = type("T", (), {"language": "Vietnamese", "language_code": "vi", "is_generated": False, "is_translatable": False})()
    monkeypatch.setattr(worker, "fetch_selected", lambda *a: (transcript, False, [{"text": "Chào", "start": 0, "duration": 1}]))
    result = worker.process_download(s, job)
    worker.apply_download_result(s, video.id, result)
    s.commit()
    subtitles = s.scalars(select(Subtitle)).all()
    assert {x.file_path for x in subtitles} == {f"Kênh/2025-03-Tiêu đề-abc/vi.{ext}" for ext in ("srt", "txt")}
    assert s.get(Video, video.id).subtitle_path == "Kênh/2025-03-Tiêu đề-abc/vi.srt"
    assert storage.resolve_subtitle_path(s.get(Video, video.id).subtitle_path).read_text(encoding="utf-8").startswith("1\n00:00:00,000")
    assert not list(data_dir.rglob("*.tmp"))
