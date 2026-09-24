"""Runtime settings shared by API and worker through the database (never returned raw by the API)."""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import PLACEHOLDER_KEYS, read_legacy_youtube_api_key
from ..database import SessionLocal
from ..models import AppSetting

YOUTUBE_API_KEY = "youtube_api_key"


def _session_scope(db: Session | None):
    return db or SessionLocal()


def get_youtube_api_key(db: Session | None = None) -> str:
    """DB value wins; otherwise import a legacy .env/env key into the DB once."""
    owns = db is None
    db = _session_scope(db)
    try:
        row = db.get(AppSetting, YOUTUBE_API_KEY)
        if row and row.value.strip().lower() not in PLACEHOLDER_KEYS:
            return row.value.strip()
        legacy = read_legacy_youtube_api_key()
        if legacy:
            _store(db, legacy)
        return legacy
    finally:
        if owns:
            db.close()


def _store(db: Session, key: str) -> None:
    try:
        row = db.get(AppSetting, YOUTUBE_API_KEY)
        if row:
            row.value = key
        else:
            db.add(AppSetting(key=YOUTUBE_API_KEY, value=key))
        db.commit()
    except IntegrityError:  # another process imported it first
        db.rollback()
        db.get(AppSetting, YOUTUBE_API_KEY).value = key
        db.commit()


def set_youtube_api_key(api_key: str, db: Session | None = None) -> None:
    key = api_key.strip()
    if len(key) < 10 or any(character in key for character in "\r\n"):
        raise ValueError("API key không hợp lệ")
    owns = db is None
    db = _session_scope(db)
    try:
        _store(db, key)
    finally:
        if owns:
            db.close()


def youtube_api_key_configured(db: Session | None = None) -> bool:
    return bool(get_youtube_api_key(db))
