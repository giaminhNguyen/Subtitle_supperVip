"""Phase 3: worker heartbeat, health, diagnostics, backup/restore, retention, scan-failure scheduling."""

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_phase1 import new_session
from test_phase2 import FakeYouTube, install, make_channel, only_run  # noqa: F401

from alembic import command
from app import database, dbtools, main, worker
from app.config import Settings, settings
from app.database import make_engine
from app.models import Channel, Job, JobLog, JobStatus, SyncRun, Video, WorkerHeartbeat
from app.services import jobs as jobs_service
from app.services import maintenance, runtime_settings, workers

BACKEND = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 25, 12, 0, 0)


def beat(db, worker_id, age_seconds, state="idle", now=NOW):
    workers.write_heartbeat(db, worker_id, now - timedelta(days=1), state, None, now=now - timedelta(seconds=age_seconds))


# ---------- heartbeat ----------
def test_fresh_heartbeat_is_running_stale_is_offline_none_is_absent(shared_db):
    db = new_session(shared_db)()
    assert workers.worker_summary(db, NOW)["status"] == "absent"
    beat(db, "w1", 5)
    summary = workers.worker_summary(db, NOW)
    assert (summary["status"], summary["active"], summary["stale"]) == ("running", 1, 0)
    assert workers.worker_summary(db, NOW + timedelta(seconds=settings.worker_stale_seconds + 1))["status"] == "offline"


def test_multiple_workers_and_a_crashed_one(shared_db):
    db = new_session(shared_db)()
    beat(db, "w1", 2, "busy")
    beat(db, "w2", 3)
    beat(db, "dead", 600)
    summary = workers.worker_summary(db, NOW)
    assert (summary["status"], summary["active"], summary["stale"], summary["busy"]) == ("running", 2, 1, 1)


def test_heartbeat_upsert_keeps_started_at_and_one_row_per_worker(shared_db):
    db = new_session(shared_db)()
    started = NOW - timedelta(hours=2)
    workers.write_heartbeat(db, "w1", started, "idle", None, now=NOW)
    workers.write_heartbeat(db, "w1", NOW, "busy", "job-1", now=NOW + timedelta(seconds=10))
    row = db.scalars(select(WorkerHeartbeat)).one()
    assert row.started_at == started and row.state == "busy" and row.current_job_id == "job-1" and row.last_heartbeat_at == NOW + timedelta(seconds=10)


def test_old_heartbeat_records_are_cleaned_but_recent_stale_ones_kept(shared_db):
    db = new_session(shared_db)()
    beat(db, "old", settings.worker_record_retention_days * 86400 + 60)
    beat(db, "recently-dead", 3600)
    beat(db, "alive", 1)
    assert workers.cleanup_worker_records(db, NOW) == 1
    assert {r.worker_id for r in db.scalars(select(WorkerHeartbeat))} == {"recently-dead", "alive"}


def test_heartbeat_thread_writes_and_removes_its_row_on_clean_stop(shared_db, monkeypatch):
    monkeypatch.setattr(settings, "worker_heartbeat_seconds", 0.05)
    thread = workers.HeartbeatThread(new_session(shared_db), "w-thread", workers.WorkerState()).start()
    db = new_session(shared_db)()
    deadline = time.time() + 5
    while time.time() < deadline and not db.scalars(select(WorkerHeartbeat)).all():
        time.sleep(0.02)
    assert [r.worker_id for r in db.scalars(select(WorkerHeartbeat))] == ["w-thread"]
    thread.stop()
    db.expire_all()
    assert db.scalars(select(WorkerHeartbeat)).all() == []


def test_hung_worker_does_not_look_alive_even_though_the_thread_runs(shared_db, monkeypatch):
    clock = [0.0]
    state = workers.WorkerState(clock=lambda: clock[0])
    thread = workers.HeartbeatThread(new_session(shared_db), "w-hung", state)
    assert thread.beat_once()  # idle and ticking
    clock[0] += settings.worker_stale_seconds + 1  # main loop stopped ticking while idle
    assert not thread.beat_once()
    state.busy("job-1")
    assert thread.beat_once()
    clock[0] += settings.job_max_runtime_seconds + 1  # one job "running" past the hard cap
    assert not thread.beat_once()
    state.tick()
    state.idle()
    assert thread.beat_once()  # recovers once the loop moves again


def test_heartbeat_interval_default_is_not_spammy_and_config_validated():
    assert Settings().worker_heartbeat_seconds >= 5
    with pytest.raises(ValueError):
        Settings(worker_heartbeat_seconds=30, worker_stale_seconds=30)
    with pytest.raises(ValueError):
        Settings(worker_heartbeat_seconds=10, worker_stale_seconds=15)
    with pytest.raises(ValueError):
        Settings(db_backup_keep_count=0)
    assert Settings(worker_heartbeat_seconds=5, worker_stale_seconds=10)


# ---------- health ----------
def no_network(monkeypatch):
    import httpx
    import requests

    def boom(*a, **k):
        raise AssertionError("health/diagnostics must not call the network")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(requests.Session, "request", boom)


def test_health_ok_when_everything_is_up(shared_db, data_dir, monkeypatch):
    no_network(monkeypatch)
    runtime_settings.set_youtube_api_key("SUPERSECRETKEY123")
    with database.SessionLocal() as db:
        workers.write_heartbeat(db, "w1", NOW, "idle", None)
    response = TestClient(main.app).get("/health")
    body = response.json()
    assert response.status_code == 200 and body["ok"] and body["status"] == "ok"
    assert (body["database"], body["storage"], body["worker"]["status"], body["youtube_api_key_configured"]) == ("ok", "ok", "running", True)
    assert "SUPERSECRETKEY123" not in response.text


def test_health_degraded_not_critical_when_worker_absent_or_stale(shared_db, data_dir):
    client = TestClient(main.app)
    absent = client.get("/health")
    assert absent.status_code == 200 and absent.json()["status"] == "degraded" and absent.json()["worker"]["status"] == "absent" and absent.json()["ok"]
    with database.SessionLocal() as db:
        workers.write_heartbeat(db, "w1", NOW, "idle", None, now=datetime.utcnow() - timedelta(hours=1))
    stale = client.get("/health").json()
    assert stale["status"] == "degraded" and stale["worker"]["status"] == "offline"


def test_health_critical_503_when_database_fails(monkeypatch):
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", lambda: Broken())
    response = TestClient(main.app).get("/health")
    assert response.status_code == 503 and response.json()["ok"] is False and response.json()["database"] == "error"


def test_health_critical_when_storage_is_unusable(shared_db, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "data_dir", blocker / "data")  # cannot be created under a file
    response = TestClient(main.app).get("/health")
    assert response.status_code == 503 and response.json()["storage"] == "error" and response.json()["status"] == "critical"


def test_health_does_not_create_files_in_data_dir(shared_db, data_dir):
    TestClient(main.app).get("/health")
    assert list(data_dir.iterdir()) == []


# ---------- diagnostics ----------
def migrated_engine(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'diag.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    monkeypatch.setitem(database.SessionLocal.kw, "bind", engine)
    return engine


def test_diagnostics_reports_state_without_secrets_or_paths(tmp_path, monkeypatch):
    engine = migrated_engine(tmp_path, monkeypatch)
    no_network(monkeypatch)
    runtime_settings.set_youtube_api_key("SUPERSECRETKEY123")
    s = new_session(engine)()
    s.add_all([Job(kind="download", status=JobStatus.queued), Job(kind="download", status=JobStatus.queued), Job(kind="download", status=JobStatus.failed, id="f1")])
    channel = Channel(youtube_channel_id="UC", title="t", url="u")
    s.add(channel)
    s.commit()
    s.add(SyncRun(channel_id=channel.id, mode="new", status="downloading"))
    s.commit()
    beat(s, "w1", 1, now=datetime.utcnow())
    beat(s, "dead", 9999, now=datetime.utcnow())
    response = TestClient(main.app).get("/api/diagnostics")
    body = response.json()
    assert response.status_code == 200
    assert body["database"]["revision"] == "0004" and body["database"]["journal_mode"] == "wal" and body["database"]["size_bytes"] > 0
    assert (body["queue"]["queued"], body["queue"]["failed"], body["queue"]["processing"]) == (2, 1, 0)
    assert (body["worker"]["status"], body["worker"]["active"], body["worker"]["stale"]) == ("running", 1, 1)
    assert body["storage"]["status"] == "ok" and body["storage"]["free_bytes"] > 0 and len(body["active_sync_runs"]) == 1
    assert body["youtube_api_key_configured"] is True and body["app"]["version"]
    assert "SUPERSECRETKEY123" not in response.text and str(tmp_path) not in response.text
    assert not list(tmp_path.glob(".health-*"))  # the write probe cleans up after itself
    engine.dispose()


# ---------- backup ----------
def make_db(path: Path, rows=3, revision="0004"):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def rows_of(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY id")]
    finally:
        conn.close()


def test_backup_skips_missing_or_empty_database(tmp_path):
    backups = tmp_path / "b"
    assert dbtools.backup_database(tmp_path / "nope.db", backups) is None and not backups.exists()
    (tmp_path / "empty.db").write_bytes(b"")
    assert dbtools.backup_database(tmp_path / "empty.db", backups) is None
    sqlite3.connect(tmp_path / "blank.db").close()
    assert dbtools.backup_database(tmp_path / "blank.db", backups) is None


def test_backup_is_a_valid_consistent_copy_even_of_a_live_wal_database(tmp_path):
    db = make_db(tmp_path / "app.db")
    live = sqlite3.connect(db)
    live.execute("INSERT INTO t (v) VALUES ('in-wal')")
    live.commit()  # connection stays open, data may sit in the WAL
    backup = dbtools.backup_database(db, tmp_path / "b", now=datetime(2026, 9, 25, 10, 15, 30))
    live.close()
    assert backup.name == "subtitle-db-20260925-101530.sqlite3"
    assert rows_of(backup) == ["row0", "row1", "row2", "in-wal"] and dbtools._integrity_ok(backup)
    assert [p.name for p in (tmp_path / "b").iterdir()] == [backup.name]  # no -wal/-shm/.partial leftovers


def test_backup_names_are_unique_within_one_second(tmp_path):
    db = make_db(tmp_path / "app.db")
    same = datetime(2026, 9, 25, 10, 0, 0)
    a = dbtools.backup_database(db, tmp_path / "b", now=same)
    b = dbtools.backup_database(db, tmp_path / "b", now=same)
    assert a != b and a.exists() and b.exists()


def test_retention_keeps_newest_n_and_failed_backup_deletes_nothing(tmp_path, monkeypatch):
    db = make_db(tmp_path / "app.db")
    directory = tmp_path / "b"
    for i in range(5):
        dbtools.backup_database(db, directory, keep=3, now=datetime(2026, 9, 25, 10, 0, i))
    names = sorted(p.name for p in dbtools.list_backups(directory))
    assert names == [f"subtitle-db-20260925-10000{i}.sqlite3" for i in (2, 3, 4)]

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(dbtools, "_copy_database", boom)
    with pytest.raises(dbtools.BackupError):
        dbtools.backup_database(db, directory, keep=1, now=datetime(2026, 9, 25, 11, 0, 0))
    assert sorted(p.name for p in dbtools.list_backups(directory)) == names  # nothing pruned on failure


def test_safety_copies_are_never_pruned_and_unrelated_files_are_ignored(tmp_path):
    db = make_db(tmp_path / "app.db")
    directory = tmp_path / "b"
    directory.mkdir()
    (directory / "subtitle-db-prerestore-20200101-000000.sqlite3").write_bytes(b"x")
    (directory / "notes.txt").write_text("keep me")
    for i in range(3):
        dbtools.backup_database(db, directory, keep=1, now=datetime(2026, 9, 25, 10, 0, i))
    assert (directory / "subtitle-db-prerestore-20200101-000000.sqlite3").exists() and (directory / "notes.txt").exists()
    assert len([p for p in dbtools.list_backups(directory) if dbtools.AUTO_RE.match(p.name)]) == 1


def test_pre_migration_backup_only_when_migrations_are_pending(tmp_path, monkeypatch):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert dbtools.pre_migration_backup(tmp_path / "m.db", directory=tmp_path / "b") is None  # no DB yet
    command.upgrade(cfg, "0003")
    assert dbtools.pre_migration_backup(tmp_path / "m.db", directory=tmp_path / "b").exists()  # 0003 -> head is pending
    command.upgrade(cfg, "head")
    assert dbtools.pre_migration_backup(tmp_path / "m.db", directory=tmp_path / "b") is None  # up to date: no churn
    assert dbtools.database_revision(dbtools.list_backups(tmp_path / "b")[0]) == "0003"  # the backup holds the pre-migration state


def test_migration_failure_leaves_a_usable_backup(tmp_path, monkeypatch):
    db = make_db(tmp_path / "app.db", revision="0001")
    backup = dbtools.pre_migration_backup(db, directory=tmp_path / "b")
    db.write_bytes(b"corrupted during a failed migration")
    assert rows_of(backup) == ["row0", "row1", "row2"] and dbtools.database_revision(backup) == "0001"


# ---------- restore ----------
def test_restore_replaces_db_keeps_safety_copy_and_backup(tmp_path):
    db = make_db(tmp_path / "app.db", rows=2, revision="0002")
    directory = tmp_path / "b"
    good = dbtools.backup_database(db, directory, now=datetime(2026, 9, 25, 9, 0, 0))
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t (v) VALUES ('after-backup')")
    conn.commit()
    conn.close()
    info = dbtools.restore_database(good, db, directory, now=datetime(2026, 9, 25, 10, 0, 0))
    assert rows_of(db) == ["row0", "row1"] and info["revision"] == "0002" and good.exists()
    assert rows_of(directory / info["safety_backup"]) == ["row0", "row1", "after-backup"]
    assert not list(tmp_path.glob("app.db-wal")) or dbtools._integrity_ok(db)


def test_corrupt_or_wrong_file_is_rejected_before_touching_the_current_db(tmp_path):
    db = make_db(tmp_path / "app.db")
    before = db.read_bytes()
    directory = tmp_path / "b"
    directory.mkdir()
    bad = directory / "subtitle-db-20260101-000000.sqlite3"
    bad.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(dbtools.RestoreError):
        dbtools.restore_database(bad, db, directory)
    with pytest.raises(dbtools.RestoreError):
        dbtools.restore_database(directory / "missing.sqlite3", db, directory)
    assert db.read_bytes() == before and rows_of(db) == ["row0", "row1", "row2"] and not list(directory.glob("subtitle-db-prerestore-*"))


def test_restore_refused_while_database_is_in_use_or_worker_alive(tmp_path):
    db = make_db(tmp_path / "app.db")
    backup = dbtools.backup_database(db, tmp_path / "b")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(dbtools.RestoreError):
            dbtools.restore_database(backup, db, tmp_path / "b")
    finally:
        holder.close()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE worker_heartbeats (last_heartbeat_at TEXT)")
    conn.execute("INSERT INTO worker_heartbeats VALUES (?)", (datetime.utcnow().isoformat(sep=" "),))
    conn.commit()
    conn.close()
    with pytest.raises(dbtools.RestoreError, match="Worker"):
        dbtools.restore_database(backup, db, tmp_path / "b")


def test_restore_accepts_only_plain_backup_names(tmp_path):
    for bad in ("../app.db", "..\\x.sqlite3", "sub/dir.sqlite3", "notes.txt"):
        with pytest.raises(dbtools.RestoreError):
            dbtools.resolve_backup(bad, tmp_path)


def test_restore_into_missing_db_works(tmp_path):
    backup = dbtools.backup_database(make_db(tmp_path / "src.db"), tmp_path / "b")
    info = dbtools.restore_database(backup, tmp_path / "new" / "app.db", tmp_path / "b")
    assert info["safety_backup"] is None and rows_of(tmp_path / "new" / "app.db") == ["row0", "row1", "row2"]


# ---------- runtime logs ----------
def test_log_rotation_archives_current_and_prunes_only_old_archives(tmp_path):
    (tmp_path / "api.out.log").write_text("current run")
    (tmp_path / "worker.err.log").write_text("")
    (tmp_path / "api.out.20200101-000000.log").write_text("ancient")
    (tmp_path / "api.out.20260920-000000.log").write_text("recent")
    result = dbtools.rotate_logs(tmp_path, retention_days=14, now=datetime(2026, 9, 25))
    names = {p.name for p in tmp_path.iterdir()}
    assert result == {"archived": 1, "deleted": 1} and "api.out.20200101-000000.log" not in names and "api.out.20260920-000000.log" in names
    assert "api.out.log" not in names and "worker.err.log" in names  # empty file untouched; previous log archived
    assert any(n.startswith("api.out.2") and n != "api.out.20260920-000000.log" for n in names)


# ---------- DB history retention ----------
def build_history(engine):
    s = new_session(engine)()
    old = NOW - timedelta(days=90)
    recent = NOW - timedelta(days=1)
    channel = Channel(youtube_channel_id="UC", title="t", url="u")
    s.add(channel)
    s.commit()
    cid = channel.id

    def run(name, status, finished, jobs):
        r = SyncRun(id=name, channel_id=cid, mode="new", status=status, started_at=old, finished_at=finished)
        s.add(r)
        s.commit()
        for i, st in enumerate(jobs):
            s.add(Video(id=f"v-{name}-{i}", channel_id=cid, youtube_video_id=f"{name}-{i}", title="t", url="u"))
            s.commit()
            j = Job(
                id=f"{name}-j{i}",
                kind="download",
                status=st,
                channel_id=cid,
                video_id=f"v-{name}-{i}",
                sync_run_id=name,
                created_at=old,
                finished_at=finished if st in (JobStatus.completed, JobStatus.failed) else None,
            )
            s.add(j)
            s.commit()
            s.add(JobLog(job_id=j.id, message="m", created_at=old))
            s.commit()

    run("old-run", "completed", old, [JobStatus.completed, JobStatus.failed])
    run("recent-run", "completed", recent, [JobStatus.completed])
    run("active-run", "downloading", None, [JobStatus.queued, JobStatus.completed])
    run("bogus-terminal-run", "partial", old, [JobStatus.processing])  # inconsistent: still has an active job
    for name, st in (("solo-old-done", JobStatus.completed), ("solo-old-queued", JobStatus.queued), ("solo-old-proc", JobStatus.processing), ("solo-old-paused", JobStatus.paused)):
        s.add(Video(id=f"v-{name}", channel_id=cid, youtube_video_id=name, title="t", url="u"))
        s.commit()
        s.add(Job(id=name, kind="download", status=st, video_id=f"v-{name}", created_at=old, finished_at=old if st == JobStatus.completed else None))
        s.add(JobLog(job_id=name, message="m", created_at=old))
    s.add(Job(id="solo-recent-done", kind="download", status=JobStatus.completed, created_at=recent, finished_at=recent))
    s.add(JobLog(message="orphan-old", created_at=old))
    s.add(JobLog(message="fresh", created_at=recent))
    s.commit()
    beat(s, "ancient-worker", 20 * 86400)
    return s


def test_retention_deletes_only_old_terminal_history(shared_db):
    s = build_history(shared_db)
    result = maintenance.run_retention(s, NOW)
    s.expire_all()
    jobs = {j.id for j in s.scalars(select(Job))}
    runs = {r.id for r in s.scalars(select(SyncRun))}
    assert runs == {"recent-run", "active-run", "bogus-terminal-run"}
    assert jobs == {"recent-run-j0", "active-run-j0", "active-run-j1", "bogus-terminal-run-j0", "solo-old-queued", "solo-old-proc", "solo-old-paused", "solo-recent-done"}
    logs = {(entry.job_id, entry.message) for entry in s.scalars(select(JobLog))}
    assert (None, "orphan-old") not in logs and (None, "fresh") in logs
    logged = {j for j, _ in logs if j}
    assert (
        logged <= jobs and {"active-run-j0", "bogus-terminal-run-j0", "solo-old-queued", "solo-old-proc", "solo-old-paused"} <= logged
    )  # active jobs keep their logs; none dangle
    assert result["runs"] == 1 and result["jobs"] == 1 and s.scalars(select(WorkerHeartbeat)).all() == []
    with shared_db.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_retention_is_idempotent_and_batched(shared_db, monkeypatch):
    monkeypatch.setattr(maintenance, "BATCH", 2)
    s = build_history(shared_db)
    first = maintenance.run_retention(s, NOW)
    second = maintenance.run_retention(s, NOW)
    assert first["runs"] == 1 and set(second.values()) == {0}
    for i in range(7):
        s.add(Job(id=f"bulk{i}", kind="download", status=JobStatus.failed, created_at=NOW - timedelta(days=99), finished_at=NOW - timedelta(days=99)))
    s.commit()
    assert maintenance.run_retention(s, NOW)["jobs"] == 7


def test_maintenance_schedule_runs_at_startup_then_only_when_due():
    now = [0.0]
    schedule = maintenance.MaintenanceSchedule(clock=lambda: now[0])
    assert schedule.due()
    schedule.done()
    assert not schedule.due()
    now[0] += settings.maintenance_interval_hours * 3600 - 1
    assert not schedule.due()
    now[0] += 2
    assert schedule.due()


def test_worker_loop_runs_maintenance_once_not_per_poll():
    calls = []
    polls = []
    schedule = maintenance.MaintenanceSchedule(clock=lambda: 0.0)
    worker.worker_loop(
        process=lambda: polls.append(1) or False, sleep=lambda s: None, poll_seconds=0, should_stop=lambda: len(polls) >= 5, maintenance=lambda: calls.append(1), schedule=schedule
    )
    assert len(calls) == 1 and len(polls) == 5


# ---------- scan-failure scheduling ----------
def test_permanently_failed_scan_pushes_next_sync_out_instead_of_looping(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["a"], fail_at_page=0))
    s, channel_id = make_channel(shared_db)
    past = datetime.utcnow() - timedelta(hours=1)
    s.execute(text("UPDATE channels SET next_sync_at = :t"), {"t": past})
    s.commit()
    with database.SessionLocal() as db:
        jobs_service.due_channel_syncs(db)
        db.commit()
    s.execute(text("UPDATE jobs SET max_attempts = 1"))
    s.commit()
    assert worker.process_one()
    s.expire_all()
    assert s.scalars(select(Job)).one().status == JobStatus.failed
    assert s.get(Channel, channel_id).next_sync_at >= datetime.utcnow() + timedelta(minutes=settings.scan_failure_retry_minutes - 1)
    assert not worker.process_one()  # the scheduler does not immediately enqueue another scan
    s.expire_all()
    assert len(s.scalars(select(Job)).all()) == 1


def test_manual_scan_failure_does_not_enable_scheduling_and_retry_still_works(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["a"], fail_at_page=0))
    s, channel_id = make_channel(shared_db)
    job = jobs_service.enqueue(s, "scan", channel_id=channel_id, payload={"mode": "new"})
    job.max_attempts = 1
    s.commit()
    job_id = job.id
    assert worker.process_one()
    s.expire_all()
    assert s.get(Channel, channel_id).next_sync_at is None
    assert TestClient(main.app).post(f"/api/jobs/{job_id}/retry").status_code == 200
    s.expire_all()
    assert s.get(Job, job_id).status == JobStatus.queued


def test_successful_scan_schedules_the_normal_interval(shared_db, monkeypatch):
    install(monkeypatch, FakeYouTube(["a"]))
    s, channel_id = make_channel(shared_db)
    jobs_service.enqueue(s, "scan", channel_id=channel_id, payload={"mode": "new"})
    s.commit()
    assert worker.process_one()
    s.expire_all()
    assert abs((s.get(Channel, channel_id).next_sync_at - datetime.utcnow()) - timedelta(hours=24)) < timedelta(minutes=1)
