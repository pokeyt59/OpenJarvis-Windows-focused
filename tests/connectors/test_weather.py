"""Tests for WeatherConnector — OpenWeatherMap API."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry


def test_weather_registered():
    """WeatherConnector is discoverable via ConnectorRegistry."""
    from openjarvis.connectors.weather import WeatherConnector

    ConnectorRegistry.register_value("weather", WeatherConnector)
    assert ConnectorRegistry.contains("weather")
    cls = ConnectorRegistry.get("weather")
    assert cls.connector_id == "weather"
    assert cls.display_name == "Weather"
    # "oauth" so /connect routes the composite "location:api_key"
    # payload through handle_callback. OpenWeatherMap uses API keys,
    # not real OAuth.
    assert cls.auth_type == "oauth"


_CURRENT_RESPONSE = {
    "main": {"temp": 62.5, "humidity": 55},
    "weather": [{"description": "clear sky"}],
    "wind": {"speed": 8.2},
}

_FORECAST_RESPONSE = {
    "list": [
        {
            "dt_txt": "2026-04-02 12:00:00",
            "main": {"temp": 64.0},
            "weather": [{"description": "few clouds"}],
        },
        {
            "dt_txt": "2026-04-02 15:00:00",
            "main": {"temp": 66.0},
            "weather": [{"description": "scattered clouds"}],
        },
    ],
}


@pytest.fixture()
def connector(tmp_path):
    """WeatherConnector with fake config file."""
    from openjarvis.connectors.weather import WeatherConnector

    config_path = tmp_path / "weather.json"
    config_path.write_text(
        '{"api_key": "fake-key", "location": "San Francisco,CA"}',
        encoding="utf-8",
    )
    return WeatherConnector(token_path=str(config_path))


def test_is_connected(connector):
    assert connector.is_connected() is True


def test_is_connected_no_file(tmp_path):
    from openjarvis.connectors.weather import WeatherConnector

    c = WeatherConnector(token_path=str(tmp_path / "missing.json"))
    assert c.is_connected() is False


def test_sync_yields_two_documents(connector):
    """Sync returns one current weather and one forecast Document."""
    with patch(
        "openjarvis.connectors.weather._weather_api_get",
        side_effect=[_CURRENT_RESPONSE, _FORECAST_RESPONSE],
    ):
        docs = list(connector.sync())

    assert len(docs) == 2
    assert all(isinstance(d, Document) for d in docs)

    current = docs[0]
    assert current.source == "weather"
    assert current.doc_type == "current"
    assert "62.5" in current.content
    assert "clear sky" in current.content
    assert "55" in current.content

    forecast = docs[1]
    assert forecast.doc_type == "forecast"
    assert "64.0" in forecast.content


def test_disconnect(connector):
    connector.disconnect()
    assert connector.is_connected() is False


@pytest.mark.parametrize(
    "location,expected",
    [
        # City strings → q lookup
        ("San Francisco,CA", {"q": "San Francisco,CA"}),
        ("London,GB", {"q": "London,GB"}),
        ("Portland,OR,US", {"q": "Portland,OR,US"}),
        # Coordinates (from the "My location" button) → lat/lon lookup
        ("37.7749,-122.4194", {"lat": "37.7749", "lon": "-122.4194"}),
        (" 51.5074 , -0.1278 ", {"lat": "51.5074", "lon": "-0.1278"}),
        # Out-of-range / non-numeric "two parts" fall back to a city lookup
        ("91.0,0.0", {"q": "91.0,0.0"}),
        ("not,coords", {"q": "not,coords"}),
    ],
)
def test_owm_query_params(location, expected):
    from openjarvis.connectors.weather import _owm_query_params

    assert _owm_query_params(location) == expected


def test_sync_uses_coords_when_location_is_latlon(tmp_path):
    """A 'lat,lon' location queries OpenWeatherMap by coordinates, not city."""
    from openjarvis.connectors.weather import WeatherConnector

    config_path = tmp_path / "weather.json"
    config_path.write_text(
        '{"api_key": "fake-key", "location": "37.7749,-122.4194"}',
        encoding="utf-8",
    )
    c = WeatherConnector(token_path=str(config_path))

    with patch(
        "openjarvis.connectors.weather._weather_api_get",
        side_effect=[_CURRENT_RESPONSE, _FORECAST_RESPONSE],
    ) as mock_get:
        docs = list(c.sync())

    assert len(docs) == 2
    # The current-weather call must use lat/lon params, never q.
    first_params = mock_get.call_args_list[0].kwargs["params"]
    assert first_params.get("lat") == "37.7749"
    assert first_params.get("lon") == "-122.4194"
    assert "q" not in first_params
