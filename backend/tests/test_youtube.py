import pytest

from app.services.youtube import parse_channel_url, parse_duration


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/@GoogleDevelopers", ("handle", "@GoogleDevelopers")),
        ("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", ("id", "UC_x5XG1OV2P6uZZ5FSM9Ttw")),
        ("https://www.youtube.com/user/Google", ("username", "Google")),
    ],
)
def test_parse_supported_channel_urls(url, expected):
    assert parse_channel_url(url) == expected


def test_rejects_non_youtube_url():
    with pytest.raises(ValueError):
        parse_channel_url("https://example.com/channel/foo")


def test_parses_iso_duration():
    assert parse_duration("PT1H2M3S") == 3723
