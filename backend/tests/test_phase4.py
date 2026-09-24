"""Phase 4 backend: paginated list endpoints and safe job retry."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.models import Channel, ChannelSettings, Job, JobLog, JobStatus, Subtitle, Video
from test_phase1 import new_session, shared_db  # noqa: F401 (fixture)

BASE = datetime(2026, 1, 1)


def seed_channel(engine, videos=0, jobs=0, logs=0):
    s = new_session(engine)()
    channel = Channel(youtube_channel_id="UC", title="t", url="u"); channel.settings = ChannelSettings(); s.add(channel); s.commit()
    for i in range(videos): s.add(Video(channel_id=channel.id, youtube_video_id=f"v{i:03}", title=f"video {i}", url="u", published_at=BASE + timedelta(days=i)))
    for i in range(jobs): s.add(Job(id=f"job{i:03}", kind="download", status=JobStatus.failed, created_at=BASE + timedelta(minutes=i)))
    for i in range(logs): s.add(JobLog(id=f"log{i:03}", message=f"m{i}", created_at=BASE + timedelta(seconds=i)))
    s.commit(); return s, channel.id


def test_jobs_pages_are_stable_complete_and_report_total(shared_db):
    seed_channel(shared_db, jobs=5); client = TestClient(main.app)
    first = client.get("/api/jobs?limit=2&offset=0"); second = client.get("/api/jobs?limit=2&offset=2"); third = client.get("/api/jobs?limit=2&offset=4")
    assert [r.headers["X-Total-Count"] for r in (first, second, third)] == ["5", "5", "5"]
    ids = [j["id"] for r in (first, second, third) for j in r.json()]
    assert ids == ["job004", "job003", "job002", "job001", "job000"]  # newest first, no duplicates or gaps across pages
    assert client.get("/api/jobs?limit=2&offset=99").json() == []
    assert client.get("/api/jobs?status=queued").headers["X-Total-Count"] == "0"


def test_limit_is_bounded(shared_db):
    client = TestClient(main.app)
    for url in ("/api/jobs?limit=501", "/api/jobs?limit=0", "/api/jobs?offset=-1", "/api/logs?limit=1000"): assert client.get(url).status_code == 422


def test_channel_videos_paginate_and_language_filter_does_not_duplicate_rows(shared_db):
    s, channel_id = seed_channel(shared_db, videos=7)
    video = s.scalars(select(Video).order_by(Video.youtube_video_id)).first()
    for fmt in ("srt", "txt", "vtt"): s.add(Subtitle(video_id=video.id, language="Vietnamese", language_code="vi", format=fmt, file_path=f"a.{fmt}"))
    s.commit(); client = TestClient(main.app)
    page = client.get(f"/api/channels/{channel_id}/videos?limit=3&offset=3")
    assert page.headers["X-Total-Count"] == "7" and [v["youtube_video_id"] for v in page.json()] == ["v003", "v002", "v001"]
    filtered = client.get(f"/api/channels/{channel_id}/videos?language=vi")
    assert filtered.headers["X-Total-Count"] == "1" and len(filtered.json()) == 1 and len(filtered.json()[0]["subtitles"]) == 3
    assert client.get(f"/api/channels/{channel_id}/videos?q=video 6").headers["X-Total-Count"] == "1"


def test_logs_paginate_and_filter_by_job(shared_db):
    seed_channel(shared_db, jobs=1, logs=4); client = TestClient(main.app)
    response = client.get("/api/logs?limit=3"); assert response.headers["X-Total-Count"] == "4" and [l["id"] for l in response.json()] == ["log003", "log002", "log001"]
    assert client.get("/api/logs?job_id=job000").headers["X-Total-Count"] == "0"


def test_cors_exposes_total_count_header(shared_db):
    response = TestClient(main.app).get("/api/jobs", headers={"Origin": "http://localhost:5173"})
    assert "X-Total-Count" in response.headers.get("access-control-expose-headers", "")


def test_retry_refuses_when_another_active_job_exists_for_the_target(shared_db):
    s, channel_id = seed_channel(shared_db)
    video = Video(channel_id=channel_id, youtube_video_id="x", title="x", url="u"); s.add(video); s.commit()
    old = Job(id="old", kind="download", status=JobStatus.failed, channel_id=channel_id, video_id=video.id, error="boom", attempts=5)
    new = Job(id="new", kind="download", status=JobStatus.queued, channel_id=channel_id, video_id=video.id); s.add_all([old, new]); s.commit()
    client = TestClient(main.app)
    assert client.post("/api/jobs/old/retry").status_code == 409
    s.expire_all(); assert s.get(Job, "old").status == JobStatus.failed
    new.status = JobStatus.completed; s.commit()
    assert client.post("/api/jobs/old/retry").status_code == 200
    s.expire_all(); retried = s.get(Job, "old"); assert (retried.status, retried.attempts, retried.error) == (JobStatus.queued, 0, None)
    assert client.post("/api/jobs/old/retry").status_code == 409  # only failed jobs can be retried
