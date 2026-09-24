from datetime import datetime
from sqlalchemy import select
from app.models import Channel, ChannelSettings, Job, JobStatus, Video, VideoStatus
from app.services import jobs as jobs_service


def test_rescan_does_not_duplicate_video(db, monkeypatch):
    channel = Channel(youtube_channel_id="UC1", title="Kênh thử", url="https://youtube.com/channel/UC1")
    channel.settings = ChannelSettings(); db.add(channel); db.commit()
    class FakeClient:
        def resolve_channel(self, _): return type("R", (), {"uploads_playlist_id": "UU1"})()
        def list_upload_pages(self, _):
            yield [{"youtube_video_id":"video-1", "metadata": {"youtube_video_id":"video-1", "title":"Video một", "url":"https://youtube.com/watch?v=video-1", "published_at":datetime(2025,1,1), "duration_seconds":100, "thumbnail_url":None, "video_type":"video"}}]
    monkeypatch.setattr(jobs_service, "YouTubeDataClient", FakeClient)
    jobs_service.scan_channel(db, channel, "new"); db.commit()
    jobs_service.scan_channel(db, channel, "new"); db.commit()
    assert len(db.scalars(select(Video)).all()) == 1
    assert len(db.scalars(select(Job).where(Job.kind == "download")).all()) == 1


def test_enqueue_prevents_duplicate_active_job(db):
    channel = Channel(youtube_channel_id="UC2", title="K", url="https://youtube.com/channel/UC2"); channel.settings=ChannelSettings()
    video = Video(channel=channel, youtube_video_id="v", title="v", url="https://youtube.com/watch?v=v")
    db.add(video); db.commit()
    first = jobs_service.enqueue(db, "download", channel_id=channel.id, video_id=video.id)
    second = jobs_service.enqueue(db, "download", channel_id=channel.id, video_id=video.id)
    db.commit()
    assert first.id == second.id and video.status == VideoStatus.queued
