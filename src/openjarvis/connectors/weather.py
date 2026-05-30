"""Weather connector — current conditions and forecast via OpenWeatherMap API.

Uses an API key stored in the connector config dir.
All API calls are in module-level functions for easy mocking in tests.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import httpx

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry

_DEFAULT_TOKEN_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "weather.json")


def _weather_api_get(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    """Call an OpenWeatherMap API endpoint."""
    resp = httpx.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _owm_query_params(location: str) -> Dict[str, str]:
    """Map a stored location to OpenWeatherMap query params.

    Accepts either a city string (``"London,GB"``) or bare coordinates
    (``"37.7749,-122.4194"``). Coordinates — produced by the "use my location"
    (Windows OS location) button — are queried via ``lat``/``lon``; anything
    else falls back to a ``q`` city lookup.
    """
    parts = [p.strip() for p in location.split(",")]
    if len(parts) == 2:
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError:
            pass
        else:
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return {"lat": f"{lat}", "lon": f"{lon}"}
    return {"q": location}


@ConnectorRegistry.register("weather")
class WeatherConnector(BaseConnector):
    """Fetch current weather and short-term forecast from OpenWeatherMap."""

    connector_id = "weather"
    # ``auth_type = "oauth"`` is what /connect uses to route the
    # composite ``location:api_key`` string through ``handle_callback``.
    # No real OAuth — OpenWeatherMap uses simple API keys — but we get
    # the validation + persistence path for free this way.
    auth_type = "oauth"
    display_name = "Weather"

    def __init__(self, *, token_path: str = _DEFAULT_TOKEN_PATH) -> None:
        self._token_path = Path(token_path)
        self._status = SyncStatus()

    def _load_config(self) -> Dict[str, str]:
        """Load API key and location from disk."""
        data = json.loads(self._token_path.read_text(encoding="utf-8"))
        return data

    def is_connected(self) -> bool:
        if not self._token_path.exists():
            return False
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            return bool(data.get("api_key"))
        except (json.JSONDecodeError, OSError):
            return False

    def disconnect(self) -> None:
        if self._token_path.exists():
            self._token_path.unlink()

    def handle_callback(self, code: str) -> None:
        """Persist ``{api_key, location}`` from the pasted ``location:key`` string.

        The frontend concatenates the two ``inputFields`` into a single
        colon-separated payload (the same trick gmail_imap uses). We
        split, validate, and probe the API once to surface bad keys /
        unknown locations before saving.

        Format: ``"<location>:<api_key>"`` — e.g.
        ``"San Francisco,CA:abc123def456..."``. Locations may contain
        commas (city,state) but not colons, so a single split on the
        FIRST colon is safe — we use ``rsplit(":", 1)`` so an
        accidental colon in the location still routes the API key to
        the right slot.
        """
        if not code or ":" not in code:
            raise ValueError(
                "Weather expects 'location:api_key' (e.g. 'San Francisco,CA:abc123...')"
            )
        location, api_key = code.rsplit(":", 1)
        location = location.strip()
        api_key = api_key.strip()
        if not location:
            raise ValueError("Empty location")
        if not api_key:
            raise ValueError("Empty API key")

        # Probe the current-weather endpoint as a one-shot validation.
        # OpenWeatherMap returns 401 for bad keys and 404 for unknown
        # locations — both worth surfacing now rather than at sync time.
        resp = httpx.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                **_owm_query_params(location),
                "appid": api_key,
                "units": "imperial",
            },
            timeout=15.0,
        )
        if resp.status_code == 401:
            raise ValueError(
                "OpenWeatherMap rejected the API key. Note: new keys can "
                "take up to 10 minutes to activate after creation."
            )
        if resp.status_code == 404:
            raise ValueError(
                f"OpenWeatherMap couldn't find location '{location}'. "
                "Try 'City,Country' (e.g. 'London,GB') or 'City,State,Country'."
            )
        resp.raise_for_status()

        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps({"api_key": api_key, "location": location}),
            encoding="utf-8",
        )
        try:
            os.chmod(self._token_path, 0o600)
        except OSError:
            pass

    def sync(
        self, *, since: Optional[datetime] = None, cursor: Optional[str] = None
    ) -> Iterator[Document]:
        """Yield Documents for current weather and forecast."""
        config = self._load_config()
        api_key = config["api_key"]
        location = config.get("location", "San Francisco,CA")

        # Current weather
        current = _weather_api_get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                **_owm_query_params(location),
                "appid": api_key,
                "units": "imperial",
            },
        )
        main = current.get("main", {})
        weather_desc = ", ".join(
            w.get("description", "") for w in current.get("weather", [])
        )
        content = (
            f"Temperature: {main.get('temp')}°F, "
            f"Conditions: {weather_desc}, "
            f"Humidity: {main.get('humidity')}%, "
            f"Wind: {current.get('wind', {}).get('speed')} mph"
        )
        yield Document(
            doc_id=f"weather-current-{location}",
            source="weather",
            doc_type="current",
            content=content,
            title=f"Current Weather — {current.get('name') or location}",
            timestamp=datetime.now(),
            metadata={
                "location": location,
                "temp": main.get("temp"),
                "conditions": weather_desc,
                "humidity": main.get("humidity"),
                "wind_speed": current.get("wind", {}).get("speed"),
            },
        )

        # Forecast (next ~12 hours, 4 x 3-hour intervals)
        forecast = _weather_api_get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={
                **_owm_query_params(location),
                "appid": api_key,
                "units": "imperial",
                "cnt": "4",
            },
        )
        summaries = []
        for entry in forecast.get("list", []):
            dt_txt = entry.get("dt_txt", "")
            temp = entry.get("main", {}).get("temp")
            desc = ", ".join(w.get("description", "") for w in entry.get("weather", []))
            summaries.append(f"{dt_txt}: {temp}°F, {desc}")
        forecast_content = "Forecast:\n" + "\n".join(summaries)

        yield Document(
            doc_id=f"weather-forecast-{location}",
            source="weather",
            doc_type="forecast",
            content=forecast_content,
            title=f"Weather Forecast — {location}",
            timestamp=datetime.now(),
            metadata={"location": location},
        )

        self._status.state = "idle"
        self._status.last_sync = datetime.now()

    def sync_status(self) -> SyncStatus:
        return self._status
