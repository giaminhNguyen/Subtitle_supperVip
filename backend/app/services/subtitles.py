import csv, json, re
from io import StringIO
from pathlib import Path
from youtube_transcript_api import YouTubeTranscriptApi
from ..config import settings
from .ratelimit import call_with_retry


class SubtitleUnavailable(Exception): pass
class LanguageUnavailable(Exception): pass
class BlockedByYouTube(Exception): pass


def _transcript_retryable(exc: Exception) -> tuple[bool, float | None]:
    """Only plain network trouble is retried in-process; IP blocks/429 are left to job-level backoff."""
    import requests
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError)), None


def choose_transcript(transcripts, languages: list[str], preference: str, allow_translation: bool):
    items = list(transcripts)
    ordered = ([x for x in items if not getattr(x, "is_generated", False)] if preference == "manual" else
               [x for x in items if getattr(x, "is_generated", False)] if preference == "auto" else items)
    if not ordered: ordered = items if preference == "any" else []
    if not ordered: raise LanguageUnavailable("Không có loại subtitle được chọn")
    for language in languages:
        if language == "original": return ordered[0], False
        for transcript in ordered:
            if getattr(transcript, "language_code", "").lower().split("-")[0] == language.lower().split("-")[0]: return transcript, False
    if allow_translation:
        for language in languages:
            if language != "original":
                for transcript in ordered:
                    if getattr(transcript, "is_translatable", False): return transcript.translate(language), True
    raise LanguageUnavailable("Có subtitle nhưng không có ngôn ngữ yêu cầu")


def fetch_selected(video_id: str, languages: list[str], preference: str, allow_translation: bool):
    try:
        api = YouTubeTranscriptApi()
        transcript, translated = choose_transcript(call_with_retry(lambda: api.list(video_id), category="transcript.list", is_retryable=_transcript_retryable), languages, preference, allow_translation)
        fetched = call_with_retry(transcript.fetch, category="transcript.fetch", is_retryable=_transcript_retryable)
        snippets = getattr(fetched, "snippets", fetched)
        return transcript, translated, [{"text": x.text if hasattr(x, "text") else x["text"], "start": x.start if hasattr(x, "start") else x["start"], "duration": x.duration if hasattr(x, "duration") else x.get("duration", 0)} for x in snippets]
    except LanguageUnavailable: raise
    except Exception as exc:
        name = exc.__class__.__name__.lower(); message = str(exc).lower()
        if "ipblocked" in name or "requestblocked" in name or "too many" in message or "429" in message: raise BlockedByYouTube(str(exc))
        if "notranscript" in name or "transcriptsdisabled" in name: raise SubtitleUnavailable(str(exc))
        raise


def available_transcripts(video_id: str) -> list[dict]:
    """Read caption tracks without writing a subtitle file."""
    try:
        tracks = call_with_retry(lambda: YouTubeTranscriptApi().list(video_id), category="transcript.list", is_retryable=_transcript_retryable)
        return [{"language": t.language, "language_code": t.language_code, "is_generated": t.is_generated,
                 "is_translatable": t.is_translatable} for t in tracks]
    except Exception as exc:
        name = exc.__class__.__name__.lower(); message = str(exc).lower()
        if "ipblocked" in name or "requestblocked" in name or "too many" in message or "429" in message: raise BlockedByYouTube(str(exc))
        if "notranscript" in name or "transcriptsdisabled" in name: raise SubtitleUnavailable(str(exc))
        raise


def safe_name(value: str, limit: int = 120) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(". ")
    return (value or "untitled")[:limit]


def stamp(seconds: float, vtt: bool = False) -> str:
    ms = round((seconds - int(seconds)) * 1000); seconds = int(seconds)
    h, seconds = divmod(seconds, 3600); m, s = divmod(seconds, 60)
    return f"{h:02}:{m:02}:{s:02}{'.' if vtt else ','}{ms:03}"


def serialize(snippets: list[dict], fmt: str) -> str:
    if fmt == "json": return json.dumps(snippets, ensure_ascii=False, indent=2)
    if fmt == "txt": return "\n".join(x["text"] for x in snippets)
    if fmt == "csv":
        result = StringIO(); writer = csv.DictWriter(result, fieldnames=["start", "duration", "text"]); writer.writeheader(); writer.writerows(snippets); return result.getvalue()
    blocks = []
    for i, x in enumerate(snippets, 1):
        line = f"{stamp(x['start'], fmt == 'vtt')} --> {stamp(x['start'] + x['duration'], fmt == 'vtt')}\n{x['text']}"
        blocks.append(line if fmt == "vtt" else f"{i}\n{line}")
    return ("WEBVTT\n\n" if fmt == "vtt" else "") + "\n\n".join(blocks)


def video_folder(channel_title: str, video) -> Path:
    month = video.published_at.strftime("%Y-%m") if video.published_at else "unknown-date"
    return settings.data_dir / safe_name(channel_title) / f"{month}-{safe_name(video.title)}-{video.youtube_video_id}"
