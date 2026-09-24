from datetime import datetime

from pydantic import BaseModel, Field

from .models import VideoStatus


class ChannelCreate(BaseModel):
    url: str = Field(min_length=10, max_length=1024)


class YouTubeApiKeyUpdate(BaseModel):
    api_key: str = Field(min_length=10, max_length=256)


class ChannelSettingsUpdate(BaseModel):
    preferred_languages: list[str] = ["vi", "en", "original"]
    subtitle_preference: str = Field(default="any", pattern="^(manual|auto|any)$")
    allow_translation: bool = True
    export_formats: list[str] = ["srt", "txt"]
    sync_interval_hours: int = Field(default=24, ge=1, le=720)


class ScanRequest(BaseModel):
    mode: str = Field(default="new", pattern="^(all|new|since|retryable)$")
    since: datetime | None = None


class ChannelOut(BaseModel):
    id: str
    youtube_channel_id: str
    url: str
    title: str
    avatar_url: str | None
    added_at: datetime
    video_count: int

    class Config:
        from_attributes = True


class SubtitleOut(BaseModel):
    id: str
    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool
    is_translated: bool
    format: str
    file_path: str

    class Config:
        from_attributes = True


class VideoOut(BaseModel):
    id: str
    youtube_video_id: str
    title: str
    url: str
    published_at: datetime | None
    duration_seconds: int | None
    thumbnail_url: str | None
    video_type: str
    status: VideoStatus
    retry_count: int
    last_error: str | None
    last_processed_at: datetime | None
    subtitle_path: str | None
    subtitles: list[SubtitleOut] = []

    class Config:
        from_attributes = True
