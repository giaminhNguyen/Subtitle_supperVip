import re
from dataclasses import dataclass
from datetime import datetime
import httpx
from dateutil.parser import isoparse
from ..config import settings
from .ratelimit import call_with_retry
from .runtime_settings import get_youtube_api_key

API = "https://www.googleapis.com/youtube/v3"
CHANNEL_RE = re.compile(r"youtube\.com/channel/([\w-]+)", re.I)
HANDLE_RE = re.compile(r"youtube\.com/@([\w.-]+)", re.I)
USER_RE = re.compile(r"youtube\.com/user/([\w.-]+)", re.I)


def request_timeout() -> httpx.Timeout:
    """Explicit connect/read/write/pool timeouts (httpx's own default is 5s and easy to forget)."""
    total = settings.request_timeout_seconds
    return httpx.Timeout(total, connect=min(10.0, total))


class YouTubeError(RuntimeError): pass


class YouTubeHTTPError(YouTubeError):
    def __init__(self, status: int, message: str, reason: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.status, self.reason, self.retry_after = status, reason, retry_after

    @property
    def retryable(self) -> bool:
        # 400/401/404 and quota/credential 403s are permanent; only rate-limit style 403s retry.
        return self.status == 429 or self.status in (500, 502, 503, 504) or (self.status == 403 and self.reason in ("rateLimitExceeded", "userRateLimitExceeded"))


def _error_reason(response: httpx.Response) -> str:
    try: return response.json()["error"]["errors"][0]["reason"]
    except Exception: return ""


def _retry_after(response: httpx.Response) -> float | None:
    try: return float(response.headers.get("Retry-After", ""))
    except ValueError: return None


def _is_retryable(exc: Exception) -> tuple[bool, float | None]:
    if isinstance(exc, YouTubeHTTPError): return exc.retryable, exc.retry_after
    return isinstance(exc, httpx.TransportError), None


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
    def __init__(self, key: str | None = None): self.key = key if key is not None else get_youtube_api_key()

    def _get(self, resource: str, params: dict) -> dict:
        if not self.key: raise YouTubeError("Chưa cấu hình YOUTUBE_API_KEY")
        def request() -> dict:
            response = httpx.get(f"{API}/{resource}", params={**params, "key": self.key}, timeout=request_timeout())
            if response.status_code >= 400:
                raise YouTubeHTTPError(response.status_code, self._redact(f"YouTube Data API {response.status_code}: {response.text[:300]}"), _error_reason(response), _retry_after(response))
            return response.json()
        try:
            return call_with_retry(request, category=f"youtube.{resource}", is_retryable=_is_retryable, redact=self._redact)
        except httpx.TransportError as exc:  # never let the request URL (which carries the key) escape
            raise YouTubeError(self._redact(f"Lỗi kết nối YouTube Data API: {type(exc).__name__}")) from None

    def _redact(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

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
