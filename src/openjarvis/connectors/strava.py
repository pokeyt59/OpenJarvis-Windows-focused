"""Strava connector — recent activities via REST API v3.

Uses OAuth2 tokens stored locally. Refresh handled automatically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import httpx

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry

_STRAVA_API_BASE = "https://www.strava.com/api/v3"
_STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
_DEFAULT_TOKEN_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "strava.json")


def _strava_api_get(
    token: str, endpoint: str, params: Optional[Dict[str, Any]] = None
) -> Any:
    """Call a Strava API v3 endpoint."""
    resp = httpx.get(
        f"{_STRAVA_API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _strava_refresh_token(
    client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Refresh an expired Strava OAuth2 token."""
    resp = httpx.post(
        _STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


@ConnectorRegistry.register("strava")
class StravaConnector(BaseConnector):
    """Sync recent activities from Strava."""

    connector_id = "strava"
    display_name = "Strava"
    auth_type = "oauth"

    def __init__(self, *, token_path: str = _DEFAULT_TOKEN_PATH) -> None:
        self._token_path = Path(token_path)
        self._status = SyncStatus()

    def _load_tokens(self) -> Dict[str, str]:
        return json.loads(self._token_path.read_text(encoding="utf-8"))

    def _save_tokens(self, tokens: Dict[str, str]) -> None:
        self._token_path.write_text(json.dumps(tokens), encoding="utf-8")

    def _get_access_token(self) -> str:
        tokens = self._load_tokens()
        return tokens["access_token"]

    def is_connected(self) -> bool:
        return self._token_path.exists()

    def disconnect(self) -> None:
        if self._token_path.exists():
            self._token_path.unlink()

    def auth_url(self) -> str:
        """Return Strava OAuth authorization URL."""
        from urllib.parse import urlencode

        from openjarvis.connectors.oauth import (
            get_client_credentials,
            get_provider_for_connector,
        )

        provider = get_provider_for_connector("strava")
        if not provider:
            return "https://www.strava.com/settings/api"
        creds = get_client_credentials(provider)
        if not creds:
            return "https://www.strava.com/settings/api"
        client_id, _ = creds
        redirect_uri = f"http://{provider.callback_host}:{provider.callback_port}{provider.callback_path}"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(provider.scopes),
        }
        return f"{provider.auth_endpoint}?{urlencode(params)}"

    def handle_callback(self, code: str) -> None:
        """Wire up Strava from either pasted credentials or an OAuth code.

        Two paths (mirrors the Spotify connector):

        1. **Catalog form path**: user pastes ``client_id:client_secret``
           — we save credentials and kick off the browser-based dance
           in a background thread via :func:`run_connector_oauth`,
           which generates state + PKCE for the request.

        2. **OAuth callback path**: a raw authorization code arrives.
           We exchange it directly.

        Detection: if ``code`` contains a colon, it's path 1.
        """
        from openjarvis.connectors.oauth import (
            _CONNECTORS_DIR,
            _exchange_token,
            get_client_credentials,
            get_provider_for_connector,
            run_connector_oauth,
            save_client_credentials,
            save_tokens,
        )

        provider = get_provider_for_connector("strava")
        if not provider:
            raise RuntimeError("Strava OAuth provider not configured")

        code = (code or "").strip()
        if not code:
            raise ValueError("Empty Strava connect input")

        # Path 1: pasted credentials
        if ":" in code:
            client_id, client_secret = code.split(":", 1)
            client_id = client_id.strip()
            client_secret = client_secret.strip()
            if not client_id or not client_secret:
                raise ValueError("Strava client_id and client_secret must both be non-empty")

            save_client_credentials(provider, client_id, client_secret)

            # Kick off OAuth in background; thread captures state+PKCE
            # round-trip via run_connector_oauth.
            import threading

            def _run() -> None:
                try:
                    run_connector_oauth("strava", client_id, client_secret)
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_run, daemon=True).start()
            return

        # Path 2: raw OAuth authorization code
        creds = get_client_credentials(provider)
        if not creds:
            raise RuntimeError(
                "Strava client credentials not configured. Paste them in "
                "the form first (as 'client_id:client_secret')."
            )
        client_id, client_secret = creds
        redirect_uri = (
            f"http://{provider.callback_host}:{provider.callback_port}"
            f"{provider.callback_path}"
        )
        tokens = _exchange_token(provider, code, client_id, client_secret, redirect_uri)
        payload = {
            "access_token": tokens.get("access_token", ""),
            "refresh_token": tokens.get("refresh_token", ""),
            "client_id": client_id,
            "client_secret": client_secret,
        }
        for filename in provider.credential_files:
            save_tokens(str(_CONNECTORS_DIR / filename), payload)

    def sync(
        self, *, since: Optional[datetime] = None, cursor: Optional[str] = None
    ) -> Iterator[Document]:
        token = self._get_access_token()
        after_epoch = int((since or datetime.now() - timedelta(days=7)).timestamp())

        activities = _strava_api_get(
            token,
            "athlete/activities",
            params={"after": str(after_epoch), "per_page": "50"},
        )

        for act in activities:
            ts = (
                datetime.fromisoformat(act["start_date_local"].replace("Z", "+00:00"))
                if "start_date_local" in act
                else datetime.now()
            )

            yield Document(
                doc_id=f"strava-{act['id']}",
                source="strava",
                doc_type=act.get("type", "Activity").lower(),
                content=json.dumps(act),
                title=act.get("name", "Untitled Activity"),
                timestamp=ts,
                metadata={
                    "distance_m": act.get("distance", 0),
                    "moving_time_s": act.get("moving_time", 0),
                    "sport_type": act.get("sport_type", ""),
                },
            )

        self._status.state = "idle"
        self._status.last_sync = datetime.now()

    def sync_status(self) -> SyncStatus:
        return self._status
