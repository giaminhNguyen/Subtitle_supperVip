import pytest

from app.services.subtitles import LanguageUnavailable, choose_transcript, serialize


class T:
    def __init__(self, code, generated=False, translatable=False): self.language_code=code; self.is_generated=generated; self.is_translatable=translatable; self.language=code
    def translate(self, language): return T(language, self.is_generated)


def test_prefers_requested_language_then_manual_type():
    chosen, translated = choose_transcript([T("en", True), T("vi", False)], ["vi", "en"], "manual", True)
    assert chosen.language_code == "vi" and not translated


def test_uses_youtube_translation_when_enabled():
    chosen, translated = choose_transcript([T("en", False, True)], ["vi"], "any", True)
    assert chosen.language_code == "vi" and translated


def test_reports_unavailable_language_without_translation():
    with pytest.raises(LanguageUnavailable): choose_transcript([T("en")], ["vi"], "any", False)


def test_writes_srt():
    assert "00:00:00,000 --> 00:00:01,250" in serialize([{"text":"Xin chào", "start":0, "duration":1.25}], "srt")
