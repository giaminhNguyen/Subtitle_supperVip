import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.config import settings
from app.database import Base, make_engine
from app.services import ratelimit


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close(); Base.metadata.drop_all(engine)


@pytest.fixture
def shared_db(tmp_path, monkeypatch):
    """One SQLite file used through database.SessionLocal, like the API + worker do."""
    engine = make_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setitem(database.SessionLocal.kw, "bind", engine)
    yield engine
    engine.dispose()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "A" / "data"; root.mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", root)
    return root


@pytest.fixture
def no_wait(monkeypatch):
    sleeps = []
    monkeypatch.setattr(settings, "requests_per_minute", 0); ratelimit.reset_youtube_limiter()
    monkeypatch.setattr(ratelimit.time, "sleep", sleeps.append)
    yield sleeps
    ratelimit.reset_youtube_limiter()
