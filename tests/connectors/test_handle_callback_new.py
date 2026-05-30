"""Tests for the new ``handle_callback`` methods added in Phase B.

Covers GitHub Notifications, Oura, Weather, News/RSS, Spotify, Strava.
Each connector validates the input against its provider's API before
persisting, so we mock httpx responses to exercise both happy and
failure paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# GitHub Notifications — single PAT
# ---------------------------------------------------------------------------


def _make_ok_resp(json_data: dict = None) -> MagicMock:
    """A fake httpx Response that's 200 and returns json_data."""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json_data or {}
    r.raise_for_status = MagicMock()
    return r


def _make_error_resp(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    return r


def test_github_handle_callback_persists_valid_token(tmp_path: Path) -> None:
    """A token that passes the /user probe should be written to disk."""
    from openjarvis.connectors.github_notifications import GitHubNotificationsConnector

    token_path = tmp_path / "github.json"
    conn = GitHubNotificationsConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_ok_resp({"login": "octocat"})):
        conn.handle_callback("ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD")

    assert token_path.exists()
    saved = json.loads(token_path.read_text())
    assert saved["token"].startswith("ghp_")


def test_github_handle_callback_rejects_empty() -> None:
    from openjarvis.connectors.github_notifications import GitHubNotificationsConnector

    conn = GitHubNotificationsConnector(token_path="/tmp/should-not-be-written.json")
    with pytest.raises(ValueError, match="[Ee]mpty"):
        conn.handle_callback("")


def test_github_handle_callback_rejects_obviously_wrong(tmp_path: Path) -> None:
    """A 15-char string with no known prefix is rejected before hitting the API."""
    from openjarvis.connectors.github_notifications import GitHubNotificationsConnector

    conn = GitHubNotificationsConnector(token_path=str(tmp_path / "github.json"))
    with pytest.raises(ValueError, match="doesn't look like"):
        conn.handle_callback("totally not a token")


def test_github_handle_callback_translates_401(tmp_path: Path) -> None:
    """401 from GitHub becomes a friendly error message, not a stack trace."""
    from openjarvis.connectors.github_notifications import GitHubNotificationsConnector

    token_path = tmp_path / "github.json"
    conn = GitHubNotificationsConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_error_resp(401)):
        with pytest.raises(ValueError, match="[Ii]nvalid or revoked"):
            conn.handle_callback("ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD")
    # Nothing persisted on failure
    assert not token_path.exists()


def test_github_handle_callback_translates_403_scope_error(tmp_path: Path) -> None:
    """403 means the token works but lacks scope — different remediation."""
    from openjarvis.connectors.github_notifications import GitHubNotificationsConnector

    token_path = tmp_path / "github.json"
    conn = GitHubNotificationsConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_error_resp(403)):
        with pytest.raises(ValueError, match="scope"):
            conn.handle_callback("ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD")


# ---------------------------------------------------------------------------
# Oura — single PAT
# ---------------------------------------------------------------------------


def test_oura_handle_callback_persists_valid_token(tmp_path: Path) -> None:
    from openjarvis.connectors.oura import OuraConnector

    token_path = tmp_path / "oura.json"
    conn = OuraConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_ok_resp({"age": 30})):
        conn.handle_callback("my-oura-pat")

    assert token_path.exists()
    saved = json.loads(token_path.read_text())
    assert saved["token"] == "my-oura-pat"


def test_oura_handle_callback_rejects_401(tmp_path: Path) -> None:
    from openjarvis.connectors.oura import OuraConnector

    token_path = tmp_path / "oura.json"
    conn = OuraConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_error_resp(401)):
        with pytest.raises(ValueError, match="[Ii]nvalid or revoked"):
            conn.handle_callback("bad-token")
    assert not token_path.exists()


# ---------------------------------------------------------------------------
# Weather — composite "location:api_key" payload
# ---------------------------------------------------------------------------


def test_weather_handle_callback_splits_location_and_key(tmp_path: Path) -> None:
    from openjarvis.connectors.weather import WeatherConnector

    token_path = tmp_path / "weather.json"
    conn = WeatherConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_ok_resp({"main": {"temp": 65}})):
        conn.handle_callback("San Francisco,US:abc123def456")

    saved = json.loads(token_path.read_text())
    assert saved["api_key"] == "abc123def456"
    assert saved["location"] == "San Francisco,US"


def test_weather_handle_callback_handles_location_with_comma(tmp_path: Path) -> None:
    """Locations like 'Portland,OR,US' have multiple commas — split must use rsplit."""
    from openjarvis.connectors.weather import WeatherConnector

    token_path = tmp_path / "weather.json"
    conn = WeatherConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_ok_resp({"main": {"temp": 60}})):
        conn.handle_callback("Portland,OR,US:my-api-key")

    saved = json.loads(token_path.read_text())
    assert saved["location"] == "Portland,OR,US"
    assert saved["api_key"] == "my-api-key"


def test_weather_handle_callback_rejects_missing_colon() -> None:
    from openjarvis.connectors.weather import WeatherConnector

    conn = WeatherConnector(token_path="/tmp/never.json")
    with pytest.raises(ValueError, match="location:api_key"):
        conn.handle_callback("just-an-api-key")


def test_weather_handle_callback_translates_401(tmp_path: Path) -> None:
    """OpenWeatherMap 401 → friendly "key not activated" message."""
    from openjarvis.connectors.weather import WeatherConnector

    token_path = tmp_path / "weather.json"
    conn = WeatherConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_error_resp(401)):
        with pytest.raises(ValueError, match="rejected the API key"):
            conn.handle_callback("London,GB:bad-key")


def test_weather_handle_callback_translates_404(tmp_path: Path) -> None:
    """OpenWeatherMap 404 means the location string isn't recognized."""
    from openjarvis.connectors.weather import WeatherConnector

    token_path = tmp_path / "weather.json"
    conn = WeatherConnector(token_path=str(token_path))

    with patch("httpx.get", return_value=_make_error_resp(404)):
        with pytest.raises(ValueError, match="couldn't find location"):
            conn.handle_callback("Notarealplace:valid-key")


# ---------------------------------------------------------------------------
# News/RSS — single feed URL with validation against the actual feed
# ---------------------------------------------------------------------------


_VALID_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item><title>Story 1</title><link>https://example.com/1</link></item>
</channel></rss>
"""


def test_rss_handle_callback_adds_first_feed(tmp_path: Path) -> None:
    from openjarvis.connectors.news_rss import NewsRSSConnector

    config_path = tmp_path / "news_rss.json"
    conn = NewsRSSConnector(config_path=str(config_path))

    with patch(
        "openjarvis.connectors.news_rss._fetch_feed", return_value=_VALID_RSS
    ):
        conn.handle_callback("https://example.com/rss.xml")

    saved = json.loads(config_path.read_text())
    assert len(saved["feeds"]) == 1
    assert saved["feeds"][0]["url"] == "https://example.com/rss.xml"
    assert saved["feeds"][0]["name"] == "example.com"


def test_rss_handle_callback_appends_second_feed(tmp_path: Path) -> None:
    """Repeated calls accumulate feeds, dedup'd by URL."""
    from openjarvis.connectors.news_rss import NewsRSSConnector

    config_path = tmp_path / "news_rss.json"
    conn = NewsRSSConnector(config_path=str(config_path))

    with patch(
        "openjarvis.connectors.news_rss._fetch_feed", return_value=_VALID_RSS
    ):
        conn.handle_callback("https://a.example/rss")
        conn.handle_callback("https://b.example/rss")

    saved = json.loads(config_path.read_text())
    urls = [f["url"] for f in saved["feeds"]]
    assert urls == ["https://a.example/rss", "https://b.example/rss"]


def test_rss_handle_callback_dedups_repeated_url(tmp_path: Path) -> None:
    from openjarvis.connectors.news_rss import NewsRSSConnector

    config_path = tmp_path / "news_rss.json"
    conn = NewsRSSConnector(config_path=str(config_path))

    with patch(
        "openjarvis.connectors.news_rss._fetch_feed", return_value=_VALID_RSS
    ):
        conn.handle_callback("https://example.com/rss")
        conn.handle_callback("https://example.com/rss")

    saved = json.loads(config_path.read_text())
    assert len(saved["feeds"]) == 1


def test_rss_handle_callback_rejects_bad_url_scheme() -> None:
    from openjarvis.connectors.news_rss import NewsRSSConnector

    conn = NewsRSSConnector(config_path="/tmp/never.json")
    with pytest.raises(ValueError, match="http"):
        conn.handle_callback("ftp://example.com/rss")


def test_rss_handle_callback_rejects_non_xml_response(tmp_path: Path) -> None:
    """If the URL responds with malformed XML, the parse should fail and we
    should give a useful error pointing the user at the right link."""
    from openjarvis.connectors.news_rss import NewsRSSConnector

    config_path = tmp_path / "news_rss.json"
    conn = NewsRSSConnector(config_path=str(config_path))

    # Genuinely-malformed XML (unclosed tag) — fails ET.fromstring()
    with patch(
        "openjarvis.connectors.news_rss._fetch_feed",
        return_value="<html><body>Not RSS</body>",
    ):
        with pytest.raises(ValueError, match="valid XML"):
            conn.handle_callback("https://example.com/")
    assert not config_path.exists()


def test_rss_handle_callback_rejects_html_that_parses(tmp_path: Path) -> None:
    """Well-formed HTML parses as XML but the root tag isn't <rss>/<feed>.

    Distinct path from the malformed-XML check above — exercises the
    "we parsed it but it's not a feed shape" branch.
    """
    from openjarvis.connectors.news_rss import NewsRSSConnector

    config_path = tmp_path / "news_rss.json"
    conn = NewsRSSConnector(config_path=str(config_path))

    with patch(
        "openjarvis.connectors.news_rss._fetch_feed",
        return_value="<html><body>Not RSS</body></html>",
    ):
        with pytest.raises(ValueError, match="doesn't look like"):
            conn.handle_callback("https://example.com/")
    assert not config_path.exists()


# ---------------------------------------------------------------------------
# Spotify — paste path detection
# ---------------------------------------------------------------------------


def test_spotify_handle_callback_with_colon_kicks_off_oauth(tmp_path: Path) -> None:
    """When code contains ':', save creds and trigger background OAuth.

    Patch at ``openjarvis.connectors.oauth.save_client_credentials``
    (its definition site) because Spotify uses a local import to avoid
    circular dependency at module load time.
    """
    from openjarvis.connectors.spotify import SpotifyConnector

    conn = SpotifyConnector(token_path=str(tmp_path / "spotify.json"))

    with patch(
        "openjarvis.connectors.oauth.save_client_credentials"
    ) as mock_save, patch("threading.Thread") as mock_thread:
        conn.handle_callback("client-id-32-chars:client-secret-32-chars")

    mock_save.assert_called_once()
    # Thread started for background OAuth dance
    mock_thread.return_value.start.assert_called_once()


def test_spotify_handle_callback_rejects_empty_after_colon() -> None:
    from openjarvis.connectors.spotify import SpotifyConnector

    conn = SpotifyConnector()
    with pytest.raises(ValueError, match="non-empty"):
        conn.handle_callback("client-id:")
    with pytest.raises(ValueError, match="non-empty"):
        conn.handle_callback(":client-secret")


def test_spotify_handle_callback_with_code_calls_exchange(tmp_path: Path) -> None:
    """A code without colon goes through the OAuth code exchange path.

    All patches target ``openjarvis.connectors.oauth.*`` because
    Spotify's handle_callback uses local imports from that module.
    """
    from openjarvis.connectors.spotify import SpotifyConnector

    conn = SpotifyConnector(token_path=str(tmp_path / "spotify.json"))

    with patch(
        "openjarvis.connectors.oauth.get_client_credentials",
        return_value=("cid", "csec"),
    ), patch(
        "openjarvis.connectors.oauth._exchange_token",
        return_value={"access_token": "a", "refresh_token": "r"},
    ) as mock_exch, patch("openjarvis.connectors.oauth.save_tokens"):
        conn.handle_callback("just-a-code-no-colon")

    mock_exch.assert_called_once()


# ---------------------------------------------------------------------------
# Strava — same two-path pattern
# ---------------------------------------------------------------------------


def test_strava_handle_callback_with_colon_kicks_off_oauth(tmp_path: Path) -> None:
    """Same local-import patching note as the Spotify test above."""
    from openjarvis.connectors.strava import StravaConnector

    conn = StravaConnector(token_path=str(tmp_path / "strava.json"))

    with patch(
        "openjarvis.connectors.oauth.save_client_credentials"
    ) as mock_save, patch("threading.Thread") as mock_thread:
        conn.handle_callback("123456:hex-secret-value")

    mock_save.assert_called_once()
    mock_thread.return_value.start.assert_called_once()


def test_strava_handle_callback_rejects_empty_input() -> None:
    from openjarvis.connectors.strava import StravaConnector

    conn = StravaConnector()
    with pytest.raises(ValueError, match="[Ee]mpty"):
        conn.handle_callback("")
