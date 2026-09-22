from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import Job, JobStatus
from app import worker


def test_temporary_failure_is_requeued_with_backoff(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}", connect_args={"check_same_thread": False}); Base.metadata.create_all(engine)
    worker.SessionLocal.configure(bind=engine)
    db = worker.SessionLocal(); job = Job(kind="download", status=JobStatus.queued, max_attempts=3); db.add(job); db.commit(); job_id=job.id; db.close()
    monkeypatch.setattr(worker, "process_download", lambda *_: (_ for _ in ()).throw(ConnectionError("temporary network")))
    assert worker.process_one()
    db = worker.SessionLocal(); saved=db.get(Job, job_id)
    assert saved.status == JobStatus.queued and saved.attempts == 1 and saved.scheduled_at > datetime.utcnow()
    db.close()
