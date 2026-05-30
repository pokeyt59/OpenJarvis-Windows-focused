"""Shared Microsoft Graph OAuth helpers for the Windows-native trio.

All Microsoft 365 connectors (OneNote, OneDrive, Microsoft To Do) authenticate
through the same Azure App Registration and share the same refresh-on-401
machinery. To avoid duplicating that code three times this module mirrors the
shape of :mod:`openjarvis.connectors.google_auth`:

* :func:`current_access_token` — read the access token off disk.
* :func:`refresh_access_token` — exchange a stored ``refresh_token`` for a
  new access token using the v2.0 token endpoint and persist it.
* :func:`call_with_refresh` — wrap any ``api_fn(token, *args, **kwargs)``
  helper with one-shot 401 auto-refresh.
* :func:`run_ms_graph_oauth_flow` — drive the full browser-based consent
  flow (open browser, catch the ``?code=`` redirect, exchange for tokens).

The Microsoft identity platform uses ``offline_access`` as the scope that
mints a refresh token, so callers *must* include ``offline_access`` in the
scopes list passed to :func:`run_ms_graph_oauth_flow` if they want the
refresh wrapper to work. We do not silently inject it because the same
helpers are used in tests that explicitly stub the token store.

Tokens are persisted via :func:`openjarvis.connectors.oauth.save_tokens` so
they share the secure-perms (``0o600``) handling with all other connectors.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from openjarvis.connectors.oauth import load_tokens, save_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Microsoft identity platform endpoints (v2.0)
# ---------------------------------------------------------------------------

# The "common" tenant accepts both personal Microsoft accounts and any
# Azure AD work/school account, which is the right default for a personal
# productivity tool that may be used with either kind of login.
MS_GRAPH_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
MS_GRAPH_AUTH_ENDPOINT = f"{MS_GRAPH_AUTHORITY}/authorize"
MS_GRAPH_TOKEN_ENDPOINT = f"{MS_GRAPH_AUTHORITY}/token"
MS_GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Default redirect URI used by the localhost callback server.
DEFAULT_REDIRECT_URI = "http://localhost:8790/callback"

# offline_access mints a refresh_token; without it the token endpoint only
# returns a 1-hour access_token and the call_with_refresh helper is useless.
MS_GRAPH_DEFAULT_SCOPES: List[str] = [
    "offline_access",
    "openid",
    "profile",
    "email",
    "Notes.Read",
    "Files.Read",
    "Tasks.Read",
]


class MSGraphAuthError(RuntimeError):
    """Raised when Microsoft credentials are missing or refresh fails."""


# ---------------------------------------------------------------------------
# Token I/O
# ---------------------------------------------------------------------------


def current_access_token(credentials_path: str) -> str:
    """Return the current access token from the credentials file (empty if absent)."""
    tokens = load_tokens(credentials_path) or {}
    return tokens.get("access_token", tokens.get("token", ""))


def refresh_access_token(
    credentials_path: str,
    *,
    scopes: Optional[List[str]] = None,
) -> str:
    """Exchange the stored ``refresh_token`` for a new ``access_token``.

    Persists the refreshed payload (keeping the ``client_id`` and
    ``client_secret`` intact so subsequent refreshes keep working) and
    returns the new access token.

    Raises :class:`MSGraphAuthError` if any required field is missing or the
    token endpoint rejects the refresh grant (typically because the user
    revoked consent in Azure AD).
    """
    tokens = load_tokens(credentials_path)
    if not tokens:
        raise MSGraphAuthError(
            f"No credentials at {credentials_path}; re-run the connector OAuth flow."
        )
    refresh_token = tokens.get("refresh_token", "")
    client_id = tokens.get("client_id", "")
    client_secret = tokens.get("client_secret", "")
    if not (refresh_token and client_id):
        # client_secret is optional for public clients but required for
        # confidential ones. Most users register a confidential client so
        # this missing-secret path is the more common failure mode.
        raise MSGraphAuthError(
            "Stored Microsoft credentials are missing refresh_token / client_id; "
            "re-run the OAuth flow to mint a fresh refresh token."
        )

    data: Dict[str, str] = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    if scopes:
        data["scope"] = " ".join(scopes)

    resp = httpx.post(MS_GRAPH_TOKEN_ENDPOINT, data=data, timeout=30.0)
    if resp.status_code != 200:
        raise MSGraphAuthError(
            f"Microsoft token refresh failed ({resp.status_code}): {resp.text[:200]}"
        )
    payload = resp.json()
    new_token = payload.get("access_token", "")
    if not new_token:
        raise MSGraphAuthError(
            "Microsoft token refresh returned 200 but no access_token in payload."
        )

    tokens["access_token"] = new_token
    # Keep the legacy "token" key in sync for parity with the Google flow.
    tokens["token"] = new_token
    if "refresh_token" in payload:
        # MS rotates refresh tokens; persist the new one so future refreshes
        # keep working past the original refresh_token's lifetime.
        tokens["refresh_token"] = payload["refresh_token"]
    if "expires_in" in payload:
        tokens["expires_in"] = payload["expires_in"]
    save_tokens(credentials_path, tokens)
    logger.info(
        "Refreshed Microsoft access token (expires_in=%s)",
        payload.get("expires_in"),
    )
    return new_token


def call_with_refresh(
    api_fn: Callable[..., Any],
    credentials_path: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke ``api_fn(token, *args, **kwargs)`` with one-shot 401 auto-refresh.

    Loads the current access token from disk, calls the helper, and if
    Microsoft Graph returns 401 (expired or revoked access token) uses the
    stored refresh_token to mint a new access_token, persists it, and
    retries the call exactly once.

    Any other ``HTTPStatusError`` propagates unchanged — transient 5xx /
    timeout retries belong further up the stack.
    """
    token = current_access_token(credentials_path)
    try:
        return api_fn(token, *args, **kwargs)
    except httpx.HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code != 401:
            raise
        logger.info(
            "MS Graph returned 401 on %s — refreshing access token and retrying.",
            getattr(api_fn, "__name__", "<api_fn>"),
        )
        new_token = refresh_access_token(credentials_path)
        return api_fn(new_token, *args, **kwargs)


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def build_ms_graph_auth_url(
    client_id: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scopes: Optional[List[str]] = None,
) -> str:
    """Build a Microsoft identity platform v2.0 consent URL.

    ``prompt=consent`` is included because rotating scopes on an existing
    grant otherwise silently returns the old (smaller) scope set without
    informing the user that consent is needed.
    """
    if scopes is None:
        scopes = MS_GRAPH_DEFAULT_SCOPES
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(scopes),
        "prompt": "consent",
    }
    return f"{MS_GRAPH_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_ms_graph_token(
    code: str,
    client_id: str,
    client_secret: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scopes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Exchange an authorization *code* for an access + refresh token pair."""
    data: Dict[str, str] = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        data["client_secret"] = client_secret
    if scopes:
        data["scope"] = " ".join(scopes)

    resp = httpx.post(MS_GRAPH_TOKEN_ENDPOINT, data=data, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def run_ms_graph_oauth_flow(
    client_id: str,
    client_secret: str,
    scopes: List[str],
    credentials_path: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> Dict[str, Any]:
    """Run the full Microsoft Graph OAuth flow.

    Steps:

    1. Build the consent URL.
    2. Start a localhost callback HTTP server on the redirect URI's port.
    3. Open the user's browser to the consent URL.
    4. Wait for the ``?code=...`` redirect.
    5. Exchange the code for ``access_token`` + ``refresh_token``.
    6. Persist the token payload (including ``client_id`` / ``client_secret``
       so future refreshes keep working) to *credentials_path*.

    Returns the raw token response from Microsoft.

    Raises :class:`RuntimeError` if the user denies consent or the callback
    server times out.
    """
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    auth_url = build_ms_graph_auth_url(
        client_id, redirect_uri=redirect_uri, scopes=scopes
    )

    auth_code: List[str] = []
    error: List[str] = []

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — required override name
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:system-ui;text-align:center;"
                    b"padding:60px'>"
                    b"<h2 style='color:#22c55e'>Microsoft account connected!</h2>"
                    b"<p>You can close this tab and return to OpenJarvis.</p>"
                    b"</body></html>"
                )
            elif "error" in params:
                error.append(params["error"][0])
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authorization failed</h2>"
                    b"<p>Please try again.</p></body></html>"
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    port = int(urlparse(redirect_uri).port or 8790)

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 120

    webbrowser.open(auth_url)

    while not auth_code and not error:
        server.handle_request()

    server.server_close()

    if error:
        raise RuntimeError(f"Microsoft OAuth authorization failed: {error[0]}")
    if not auth_code:
        raise RuntimeError("Microsoft OAuth authorization timed out")

    tokens = exchange_ms_graph_token(
        auth_code[0],
        client_id,
        client_secret,
        redirect_uri=redirect_uri,
        scopes=scopes,
    )

    payload = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 3600),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    save_tokens(credentials_path, payload)
    return tokens


__all__ = [
    "MSGraphAuthError",
    "MS_GRAPH_API_BASE",
    "MS_GRAPH_AUTH_ENDPOINT",
    "MS_GRAPH_TOKEN_ENDPOINT",
    "MS_GRAPH_DEFAULT_SCOPES",
    "DEFAULT_REDIRECT_URI",
    "current_access_token",
    "refresh_access_token",
    "call_with_refresh",
    "build_ms_graph_auth_url",
    "exchange_ms_graph_token",
    "run_ms_graph_oauth_flow",
]
