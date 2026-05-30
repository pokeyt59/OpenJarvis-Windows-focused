"""Spotify connector — recently played tracks via Spotify Web API.

Uses OAuth2 tokens stored locally. Requires user-read-recently-played scope.
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

_SPOTIFY_API_BASE = "https://api.spotify.com/v1"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_DEFAULT_TOKEN_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "spotify.json")


def _spotify_api_get(
    token: str, endpoint: str, params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Call a Spotify Web API endpoint."""
    resp = httpx.get(
        f"{_SPOTIFY_API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


@ConnectorRegistry.register("spotify")
class SpotifyConnector(BaseConnector):
    """Sync recently played tracks from Spotify."""

    connector_id = "spotify"
    display_name = "Spotify"
    auth_type = "oauth"

    def __init__(self, *, token_path: str = _DEFAULT_TOKEN_PATH) -> None:
        self._token_path = Path(token_path)
        self._status = SyncStatus()

    def _load_tokens(self) -> Dict[str, str]:
        return json.loads(self._token_path.read_text(encoding="utf-8"))

    def _get_access_token(self) -> str:
        return self._load_tokens()["access_token"]

    def is_connected(self) -> bool:
        return self._token_path.exists()

    def disconnect(self) -> None:
        if self._token_path.exists():
            self._token_path.unlink()

    def auth_url(self) -> str:
        """Return Spotify OAuth authorization URL."""
        from urllib.parse import urlencode

        from openjarvis.connectors.oauth import (
            get_client_credentials,
            get_provider_for_connector,
        )

        provider = get_provider_for_connector("spotify")
        if not provider:
            return "https://developer.spotify.com/dashboard"
        creds = get_client_credentials(provider)
        if not creds:
            return "https://developer.spotify.com/dashboard"
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
        """Wire up Spotify from either pasted credentials or an OAuth code.

        Two paths:

        1. **Catalog form path**: user pastes ``client_id:client_secret``
           into the StepByStepPanel. We detect the colon, save those
           credentials via :func:`save_client_credentials`, then launch
           the full browser-based OAuth dance in a background thread
           (via :func:`run_connector_oauth`, which generates a fresh
           state + PKCE pair).

        2. **OAuth callback path**: an OAuth authorization code arrives
           on its own. We exchange it directly. Used by the FastAPI
           ``/oauth/callback`` route after a hardened state+PKCE flow.

        The two paths look identical from the connect endpoint's point
        of view — we just inspect ``code`` to decide which.
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

        provider = get_provider_for_connector("spotify")
        if not provider:
            raise RuntimeError("Spotify OAuth provider not configured")

        code = (code or "").strip()
        if not code:
            raise ValueError("Empty Spotify connect input")

        # Path 1: pasted credentials. Spotify client IDs and secrets
        # are 32-char hex strings; auth codes are different shape but
        # the unambiguous signal is the colon — OAuth codes never
        # contain one.
        if ":" in code:
            client_id, client_secret = code.split(":", 1)
            client_id = client_id.strip()
            client_secret = client_secret.strip()
            if not client_id or not client_secret:
                raise ValueError("Spotify client_id and client_secret must both be non-empty")

            save_client_credentials(provider, client_id, client_secret)

            # Kick off the browser-based OAuth dance in a background
            # thread so /connect can return immediately. The thread
            # spins up a local callback server on
            # ``provider.callback_port`` (8888 for Spotify), opens the
            # browser, captures the code + state, verifies state +
            # PKCE, then writes the tokens to disk.
            import threading

            def _run() -> None:
                try:
                    run_connector_oauth("spotify", client_id, client_secret)
                except Exception:  # noqa: BLE001
                    # Errors surface to the user via the eventual sync
                    # failure — there's no return channel here.
                    pass

            threading.Thread(target=_run, daemon=True).start()
            return

        # Path 2: raw OAuth authorization code. Exchange it directly.
        creds = get_client_credentials(provider)
        if not creds:
            raise RuntimeError(
                "Spotify client credentials not configured. Paste them in "
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
        after_ms = int((since or datetime.now() - timedelta(days=1)).timestamp() * 1000)

        data = _spotify_api_get(
            token,
            "me/player/recently-played",
            params={"limit": "50", "after": str(after_ms)},
        )

        for item in data.get("items", []):
            track = item.get("track", {})
            played_at = item.get("played_at", "")
            artists = ", ".join(a["name"] for a in track.get("artists", []))

            ts = (
                datetime.fromisoformat(played_at.replace("Z", "+00:00"))
                if played_at
                else datetime.now()
            )

            yield Document(
                doc_id=f"spotify-{track.get('id', '')}-{played_at}",
                source="spotify",
                doc_type="recently_played",
                content=json.dumps(item),
                title=f"{track.get('name', 'Unknown')} — {artists}",
                author=artists,
                timestamp=ts,
                url=track.get("external_urls", {}).get("spotify", ""),
                metadata={
                    "track_name": track.get("name", ""),
                    "album": track.get("album", {}).get("name", ""),
                    "duration_ms": track.get("duration_ms", 0),
                },
            )

        self._status.state = "idle"
        self._status.last_sync = datetime.now()

    def sync_status(self) -> SyncStatus:
        return self._status
