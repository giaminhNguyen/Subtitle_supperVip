import re
from dataclasses import dataclass
from datetime import datetime
import httpx
from dateutil.parser import isoparse
from ..config import settings

API = "https://www.googleapis.com/youtube/v3"
CHANNEL_RE = re.compile(r"youtube\.com/channel/([\w-]+)", re.I)
HANDLE_RE = re.compile(r"youtube\.com/@([\w.-]+)", re.I)
USER_RE = re.compile(r"youtube\.com/user/([\w.-]+)", re.I)


class YouTubeError(RuntimeError): pass


@dataclass
class ResolvedChannel:
    channel_id: str
    title: str
    avatar_url: str | None
    uploads_playlist_id: str


def parse_channel_url(url: str) -> tuple[str, str]:
    """Return an API lookup key. Custom `/c/` URLs need API search fallback."""
    clean = url.strip().split("?")[0].rstrip("/")
    if m := CHANNEL_RE.search(clean): return "id", m.group(1)
    if m := HANDLE_RE.search(clean): return "handle", "@" + m.group(1)
    if m := USER_RE.search(clean): return "username", m.group(1)
    if re.search(r"youtube\.com/(c|[\w.-]+)", clean, re.I): return "search", clean.rsplit("/", 1)[-1]
    raise ValueError("URL kênh YouTube không hợp lệ")


class YouTubeDataClient:
    def __init__(self, key: str | None = None): self.key = key if key is not None else settings.youtube_api_key

    def _get(self, resource: str, params: dict) -> dict:
        if not self.key: raise YouTubeError("Chưa cấu hình YOUTUBE_API_KEY")
        response = httpx.get(f"{API}/{resource}", params={**params, "key": self.key}, timeout=30)
        if response.status_code >= 400: raise YouTubeError(f"YouTube Data API {response.status_code}: {response.text[:300]}")
        return response.json()

    def resolve_channel(self, url: str) -> ResolvedChannel:
        key, value = parse_channel_url(url)
        params = {"part": "snippet,contentDetails", "maxResults": 1}
        if key == "id": params["id"] = value
        elif key == "handle": params["forHandle"] = value
        elif key == "username": params["forUsername"] = value
        else:
            found = self._get("search", {"part": "snippet", "type": "channel", "q": value, "maxResults": 1}).get("items", [])
            if not found: raise YouTubeError("Không tìm thấy kênh")
            params["id"] = found[0]["snippet"]["channelId"]
        items = self._get("channels", params).get("items", [])
        if not items: raise YouTubeError("Không tìm thấy kênh hoặc kênh không công khai")
        item = items[0]; thumbs = item["snippet"].get("thumbnails", {})
        avatar = (thumbs.get("high") or thumbs.get("default") or {}).get("url")
        return ResolvedChannel(item["id"], item["snippet"]["title"], avatar, item["contentDetails"]["relatedPlaylists"]["uploads"])

    def list_uploads(self, uploads_playlist_id: str):
        token = None
        while True:
            response = self._get("playlistItems", {"part": "snippet,contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50, **({"pageToken": token} if token else {})})
            ids = [x["contentDetails"]["videoId"] for x in response.get("items", []) if x.get("contentDetails", {}).get("videoId")]
            details = self._video_details(ids)
            for entry in response.get("items", []):
                video_id = entry["contentDetails"].get("videoId")
                if video_id and video_id in details: yield details[video_id]
            token = response.get("nextPageToken")
            if not token: break

    def _video_details(self, ids: list[str]) -> dict:
        if not ids: return {}
        items = self._get("videos", {"part": "snippet,contentDetails,liveStreamingDetails", "id": ",".join(ids), "maxResults": 50}).get("items", [])
        out = {}
        for x in items:
            s, c = x["snippet"], x.get("contentDetails", {})
            secs = parse_duration(c.get("duration", "PT0S"))
            video_type = "live" if x.get("liveStreamingDetails") else ("short" if secs <= 60 else "video")
            thumb = (s.get("thumbnails", {}).get("high") or s.get("thumbnails", {}).get("default") or {}).get("url")
            out[x["id"]] = {"youtube_video_id": x["id"], "title": s["title"], "url": f"https://www.youtube.com/watch?v={x['id']}", "published_at": isoparse(s["publishedAt"]).replace(tzinfo=None), "duration_seconds": secs, "thumbnail_url": thumb, "video_type": video_type}
        return out


def parse_duration(value: str) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m: return 0
    h, minute, second = (int(x or 0) for x in m.groups())
    return h * 3600 + minute * 60 + second
