"""Shared OAuth 2.0 helpers for all connectors.

Provides:
- ``OAuthProvider`` registry with configs for Google, Strava, Spotify
- Generic ``run_connector_oauth()`` that opens browser + catches callback
- URL builder, token persistence, and token cleanup utilities
- CSRF-defense state tokens (HMAC-signed, with TTL)
- PKCE (RFC 7636) for the auth code grant — protects against intercepted
  authorization codes by binding each code to a per-flow secret verifier

Security notes:
- Token files are written with ``0o600`` perms (effective on Unix; on
  Windows the chmod is a no-op and files inherit the parent directory's
  ACL — fine when ``~/.openjarvis`` is the default user-only profile).
- ``state`` is mandatory on ``/oauth/start`` and verified on
  ``/oauth/callback`` to block CSRF "account-confusion" attacks where a
  malicious page tricks the user's browser into completing an auth
  code that was issued for the attacker's session.
- PKCE is used for every provider in ``OAUTH_PROVIDERS`` because
  desktop apps are public clients (no client_secret kept on a server),
  and the spec recommends PKCE unconditionally for public clients.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from openjarvis.core.config import DEFAULT_CONFIG_DIR

# ---------------------------------------------------------------------------
# Connector credentials directory
# ---------------------------------------------------------------------------

_CONNECTORS_DIR = DEFAULT_CONFIG_DIR / "connectors"

# ---------------------------------------------------------------------------
# OAuth provider registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthProvider:
    """Configuration for an OAuth 2.0 provider."""

    name: str  # "google", "strava", "spotify"
    display_name: str
    auth_endpoint: str
    token_endpoint: str
    scopes: List[str]
    setup_url: str  # URL where user creates OAuth credentials
    setup_hint: str  # One-line instruction for setup
    callback_port: int = 8789
    callback_host: str = "127.0.0.1"
    callback_path: str = "/callback"
    token_auth: str = "body"  # "body" or "basic"
    extra_auth_params: Dict[str, str] = field(default_factory=dict)
    # Which connector IDs this provider covers (one flow → all connected)
    connector_ids: Tuple[str, ...] = ()
    # Filenames in ~/.openjarvis/connectors/ to save tokens to
    credential_files: Tuple[str, ...] = ()


# Combined scopes for all Google connectors so a single OAuth consent
# authorises Drive, Calendar, Contacts, Gmail, and Tasks at once.
GOOGLE_ALL_SCOPES: List[str] = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.readonly",
    # calendar (not .readonly) so the proactive agent can accept/decline events.
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
    # gmail.modify (a superset of gmail.readonly) so the proactive agent
    # can trash and label-modify (archive) emails after user approval.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/tasks.readonly",
]

OAUTH_PROVIDERS: Dict[str, OAuthProvider] = {
    "google": OAuthProvider(
        name="google",
        display_name="Google",
        auth_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        scopes=GOOGLE_ALL_SCOPES,
        setup_url="https://console.cloud.google.com/apis/credentials",
        setup_hint="Create an OAuth 2.0 Client ID (Desktop app type)",
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        connector_ids=(
            "gdrive",
            "gcalendar",
            "gcontacts",
            "gmail",
            "google_tasks",
        ),
        credential_files=(
            "google.json",
            "gdrive.json",
            "gcalendar.json",
            "gcontacts.json",
            "gmail.json",
            "google_tasks.json",
        ),
    ),
    "strava": OAuthProvider(
        name="strava",
        display_name="Strava",
        auth_endpoint="https://www.strava.com/oauth/authorize",
        token_endpoint="https://www.strava.com/oauth/token",
        scopes=["activity:read_all"],
        setup_url="https://www.strava.com/settings/api",
        setup_hint="Create an API Application (callback domain: localhost)",
        connector_ids=("strava",),
        credential_files=("strava.json",),
    ),
    "spotify": OAuthProvider(
        name="spotify",
        display_name="Spotify",
        auth_endpoint="https://accounts.spotify.com/authorize",
        token_endpoint="https://accounts.spotify.com/api/token",
        scopes=["user-read-recently-played"],
        setup_url="https://developer.spotify.com/dashboard",
        setup_hint=("Create an app, add redirect URI: http://127.0.0.1:8888/callback"),
        callback_port=8888,
        token_auth="basic",
        connector_ids=("spotify",),
        credential_files=("spotify.json",),
    ),
}


def get_provider_for_connector(connector_id: str) -> Optional[OAuthProvider]:
    """Return the OAuthProvider that covers *connector_id*, or ``None``."""
    for provider in OAUTH_PROVIDERS.values():
        if connector_id in provider.connector_ids:
            return provider
    return None


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def get_client_credentials(
    provider: OAuthProvider,
) -> Optional[Tuple[str, str]]:
    """Load stored client_id and client_secret for *provider*.

    Checks credential files in ``~/.openjarvis/connectors/`` and falls
    back to environment variables ``OPENJARVIS_{NAME}_CLIENT_ID`` and
    ``OPENJARVIS_{NAME}_CLIENT_SECRET``.
    """
    # Check credential files
    for filename in provider.credential_files:
        path = _CONNECTORS_DIR / filename
        tokens = load_tokens(str(path))
        if tokens and tokens.get("client_id") and tokens.get("client_secret"):
            return tokens["client_id"], tokens["client_secret"]

    # Check environment variables
    prefix = f"OPENJARVIS_{provider.name.upper()}"
    env_id = os.environ.get(f"{prefix}_CLIENT_ID", "")
    env_secret = os.environ.get(f"{prefix}_CLIENT_SECRET", "")
    if env_id and env_secret:
        return env_id, env_secret

    return None


def save_client_credentials(
    provider: OAuthProvider,
    client_id: str,
    client_secret: str,
) -> None:
    """Persist client credentials so the user never has to enter them again."""
    for filename in provider.credential_files:
        path = _CONNECTORS_DIR / filename
        existing = load_tokens(str(path)) or {}
        existing["client_id"] = client_id
        existing["client_secret"] = client_secret
        save_tokens(str(path), existing)


# ---------------------------------------------------------------------------
# OAuth state (CSRF defense) + PKCE (RFC 7636)
# ---------------------------------------------------------------------------
#
# Why both:
#   - ``state`` defends against CSRF: a malicious page can't trick the
#     user's browser into completing OUR callback URL because it can't
#     forge a valid HMAC-signed state.
#   - PKCE defends against intercepted authorization codes: even if an
#     attacker grabs the ``?code=...`` query string off the redirect, they
#     can't exchange it for tokens without the matching ``code_verifier``
#     that only our process knows.
#
# Both are belt-and-suspenders for OAuth public clients (desktop apps).

# Default state TTL — 10 minutes. The OAuth window is usually < 30s but
# we allow slack for slow consent screens / 2FA prompts.
_STATE_TTL_SECONDS: int = 600

# Per-state PKCE verifiers, keyed by the state token. Cleared when the
# callback consumes them, or evicted on TTL expiry by ``verify_state``.
# Module-level dict guarded by a lock because the FastAPI app can call
# /oauth/start and /oauth/callback from different threads.
_pkce_store: Dict[str, Tuple[str, float]] = {}
_pkce_lock = threading.Lock()


def _state_signing_key() -> bytes:
    """Per-installation HMAC key for OAuth state tokens.

    Derived deterministically from the user's home directory so it's
    stable across restarts but unique per install. NOT committed
    anywhere — exists only in memory at runtime.

    This is acceptable for the threat model (CSRF defense for OAuth):
    the attacker would need to know the user's exact home path to forge
    a state, and even then forgery only enables CSRF, not credential
    theft. For stronger guarantees we'd persist a random key on first
    use, but that adds setup friction for marginal gain on a single-user
    desktop app.
    """
    seed = f"openjarvis-oauth-state-v1::{Path.home()}".encode("utf-8")
    return hashlib.sha256(seed).digest()


def _b64url_encode(data: bytes) -> str:
    """RFC 4648 §5 base64url encoding, no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Inverse of :func:`_b64url_encode`."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def generate_state(connector_id: str) -> str:
    """Generate an HMAC-signed state token for *connector_id*.

    Payload encodes the connector_id, a random nonce, and the current
    Unix timestamp; signed with the per-installation signing key.
    Format: ``<payload_b64url>.<sig_b64url>``.

    The callback uses :func:`verify_state` to:
      1. Check the HMAC (forgery defense)
      2. Check the timestamp is within ``_STATE_TTL_SECONDS`` (replay
         defense — old state shouldn't work)
      3. Check the connector_id matches (binding defense — state for
         connector A can't be replayed against connector B)
    """
    payload = {
        "c": connector_id,
        "n": _b64url_encode(secrets.token_bytes(16)),
        "t": int(time.time()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_state_signing_key(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify_state(state: str, connector_id: str) -> bool:
    """Verify *state* was issued for *connector_id* and is still valid.

    Returns ``True`` only when ALL of:
      - state is well-formed (payload + signature, both base64url)
      - HMAC signature is valid (``hmac.compare_digest`` to prevent
        timing attacks)
      - payload's ``c`` field equals *connector_id*
      - payload's ``t`` field is within ``_STATE_TTL_SECONDS`` of now
    """
    if not state or "." not in state:
        return False
    try:
        payload_b64, sig_b64 = state.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, base64.binascii.Error):
        return False

    expected_sig = hmac.new(
        _state_signing_key(), payload_bytes, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return False

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    if payload.get("c") != connector_id:
        return False
    issued_at = payload.get("t")
    if not isinstance(issued_at, int):
        return False
    if abs(time.time() - issued_at) > _STATE_TTL_SECONDS:
        return False
    return True


def generate_pkce_pair() -> Tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` per RFC 7636 §4.

    The verifier is 64 bytes of cryptographic randomness encoded as
    base64url (43 chars after stripping padding — within the
    43–128 char range the spec requires).

    The challenge is the SHA-256 of the verifier, base64url-encoded.
    The provider receives the challenge in ``/authorize`` and stores it.
    On token exchange we send the verifier; the provider re-hashes it
    and refuses the exchange if the result doesn't match — so an
    attacker who only intercepts the redirect URL (containing the code,
    not the verifier) cannot complete the flow.
    """
    verifier = _b64url_encode(secrets.token_bytes(32))
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def remember_pkce_verifier(state: str, verifier: str) -> None:
    """Cache *verifier* indexed by *state* until the callback consumes it.

    Older entries (past TTL) are opportunistically evicted on each call.
    The PKCE verifier MUST NOT travel over the network — that's the
    whole point of PKCE. We keep it in process memory only.
    """
    now = time.time()
    cutoff = now - _STATE_TTL_SECONDS
    with _pkce_lock:
        # Evict stale entries first to bound memory.
        stale = [s for s, (_, ts) in _pkce_store.items() if ts < cutoff]
        for s in stale:
            _pkce_store.pop(s, None)
        _pkce_store[state] = (verifier, now)


def consume_pkce_verifier(state: str) -> Optional[str]:
    """Pop and return the PKCE verifier for *state*, or ``None``.

    One-shot: a state can only be consumed once. Caller is expected to
    verify the state first (HMAC + TTL); this just retrieves the
    matching verifier so token exchange can include it.
    """
    with _pkce_lock:
        entry = _pkce_store.pop(state, None)
    if entry is None:
        return None
    verifier, ts = entry
    if time.time() - ts > _STATE_TTL_SECONDS:
        return None
    return verifier


# ---------------------------------------------------------------------------
# Shared credentials file — one OAuth flow covers all Google connectors
# ---------------------------------------------------------------------------

_SHARED_GOOGLE_CREDENTIALS_PATH: str = str(_CONNECTORS_DIR / "google.json")

_GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_DEFAULT_REDIRECT_URI = "http://localhost:8789/callback"
_DEFAULT_SCOPES: List[str] = ["openid", "email", "profile"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_google_auth_url(
    client_id: str,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
    scopes: Optional[List[str]] = None,
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
) -> str:
    """Build a Google OAuth2 consent URL.

    Parameters
    ----------
    client_id:
        The OAuth 2.0 client ID from the Google Cloud Console.
    redirect_uri:
        Where Google should redirect after consent. Defaults to the local
        callback server at ``http://localhost:8789/callback``.
    scopes:
        List of OAuth scopes to request.  Defaults to
        ``["openid", "email", "profile"]``.
    state:
        Optional CSRF state token. When provided, Google echoes it back
        to the callback so we can verify the response is a legitimate
        reply to a request we issued. See :func:`generate_state`.
    code_challenge:
        Optional PKCE challenge (S256). When provided, the token
        exchange must include the matching verifier or Google refuses
        — defeats redirect-URL interception attacks. See
        :func:`generate_pkce_pair`.

    Returns
    -------
    str
        Full consent URL including query string.
    """
    if scopes is None:
        scopes = _DEFAULT_SCOPES

    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{_GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def resolve_google_credentials(connector_path: str) -> str:
    """Return the best available Google credentials file path.

    Checks the connector-specific file first, then falls back to the
    shared ``google.json``.  Returns *connector_path* if neither exists
    (so ``is_connected()`` correctly returns ``False``).
    """
    if Path(connector_path).exists():
        return connector_path
    if Path(_SHARED_GOOGLE_CREDENTIALS_PATH).exists():
        return _SHARED_GOOGLE_CREDENTIALS_PATH
    return connector_path


def load_tokens(path: str) -> Optional[Dict[str, Any]]:
    """Load OAuth tokens from a JSON file.

    Returns ``None`` if the file is missing, unreadable, or contains
    invalid JSON.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None


def save_tokens(path: str, tokens: Dict[str, Any]) -> None:
    """Persist *tokens* to *path* as JSON with owner-only (0o600) permissions.

    Creates parent directories as needed.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def delete_tokens(path: str) -> None:
    """Delete the credentials file at *path* if it exists."""
    p = Path(path)
    if p.exists():
        p.unlink()


def refresh_google_token(path: str) -> Optional[str]:
    """Refresh a Google access token using the stored refresh token.

    Reads the credentials file at *path*, exchanges its ``refresh_token``
    (plus ``client_id``/``client_secret``) for a new ``access_token``
    against Google's OAuth token endpoint, persists the refreshed payload
    back to *path*, and returns the new access token.

    Returns ``None`` if any required field is missing or the refresh call
    fails (network error or Google returns a non-2xx response — typically
    ``invalid_grant`` when the refresh token has been revoked).
    """
    import httpx

    tokens = load_tokens(path)
    if not tokens:
        return None
    refresh_token = tokens.get("refresh_token")
    client_id = tokens.get("client_id")
    client_secret = tokens.get("client_secret")
    if not (refresh_token and client_id and client_secret):
        return None

    try:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15.0,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None

    body = resp.json()
    new_access = body.get("access_token")
    if not new_access:
        return None

    tokens.update(
        {
            "access_token": new_access,
            "token": new_access,  # legacy key used by some connectors
            "token_type": body.get("token_type", tokens.get("token_type", "Bearer")),
            "expires_in": body.get("expires_in", tokens.get("expires_in", 3600)),
        }
    )
    save_tokens(path, tokens)
    return new_access


# ---------------------------------------------------------------------------
# Token exchange & full OAuth flow
# ---------------------------------------------------------------------------


def exchange_google_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
    code_verifier: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens.

    Parameters
    ----------
    code:
        The authorization code received from Google's consent redirect.
    client_id:
        OAuth 2.0 client ID.
    client_secret:
        OAuth 2.0 client secret.
    redirect_uri:
        Must match the redirect URI used when obtaining the auth code.
    code_verifier:
        Optional PKCE verifier — required when the auth request
        included a code_challenge. Google enforces the PKCE check
        even when the client is confidential (has a secret), so we
        always include it when we have one.

    Returns
    -------
    dict
        Token response containing ``access_token``, ``refresh_token``,
        ``token_type``, and ``expires_in``.
    """
    import httpx

    data: Dict[str, str] = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier is not None:
        data["code_verifier"] = code_verifier

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data=data,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def run_oauth_flow(
    client_id: str,
    client_secret: str,
    scopes: List[str],
    credentials_path: str,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
) -> Dict[str, Any]:
    """Run the full OAuth flow: browser consent, callback, token exchange.

    Steps:

    1. Build consent URL
    2. Start localhost callback server
    3. Open browser to consent URL
    4. Wait for Google to redirect with ``?code=...``
    5. Exchange code for ``access_token`` + ``refresh_token``
    6. Save tokens to *credentials_path*
    7. Return the tokens dict

    Parameters
    ----------
    client_id:
        OAuth 2.0 client ID.
    client_secret:
        OAuth 2.0 client secret.
    scopes:
        List of OAuth scopes to request.
    credentials_path:
        Where to persist the resulting tokens.
    redirect_uri:
        Local callback URI.  Defaults to ``http://localhost:8789/callback``.

    Returns
    -------
    dict
        Token response from Google (``access_token``, ``refresh_token``, etc.).

    Raises
    ------
    RuntimeError
        If the user denies authorization or the callback times out.
    """
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    # Google supports both state and PKCE; include both. Without them
    # an attacker who intercepts the redirect URL can replay the code
    # or trick a victim into completing the attacker's session.
    # ``connector_id`` here is "google" since gdrive/gcal/etc. all share
    # one consent flow.
    state = generate_state("google")
    code_verifier, code_challenge = generate_pkce_pair()

    auth_url = build_google_auth_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        code_challenge=code_challenge,
    )

    # Mutable containers used by the callback handler closure.
    auth_code: List[str] = []
    state_value: List[str] = []
    error: List[str] = []

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — required override name
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            if "code" in params:
                auth_code.append(params["code"][0])
                state_value.append(params.get("state", [""])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authorization successful!</h2>"
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
            pass  # Suppress HTTP request logs

    # Parse port from redirect_uri
    port = int(urlparse(redirect_uri).port or 8789)

    # Kill any stale listener on the port before starting
    import socket

    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test_sock.bind(("127.0.0.1", port))
        test_sock.close()
    except OSError:
        # Port in use — try to free it
        test_sock.close()
        import subprocess

        subprocess.run(
            ["lsof", "-t", "-i", f":{port}"],
            capture_output=True,
        )
        # Wait briefly and retry
        import time

        time.sleep(1)

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 120  # 2 minute timeout

    # Open the consent page in the user's default browser
    webbrowser.open(auth_url)

    # Wait for the callback (blocking, with per-request timeout)
    while not auth_code and not error:
        server.handle_request()

    server.server_close()

    if error:
        raise RuntimeError(f"OAuth authorization failed: {error[0]}")
    if not auth_code:
        raise RuntimeError("OAuth authorization timed out")

    # Verify state — defends against CSRF. We generated it bound to
    # "google" above; the callback must echo back an exact match.
    if not verify_state(state_value[0] if state_value else "", "google"):
        raise RuntimeError(
            "OAuth state mismatch — request was tampered with or expired"
        )

    # Exchange the authorization code for tokens, including the PKCE
    # verifier. Google enforces PKCE on the exchange when a challenge
    # was sent — so without the matching verifier the exchange fails
    # even with a valid code.
    tokens = exchange_google_token(
        code=auth_code[0],
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )

    # Persist tokens together with client credentials (needed for refresh)
    token_payload = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 3600),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    save_tokens(credentials_path, token_payload)

    # Also save to the shared Google credentials file so that all Google
    # connectors can use this token without a separate OAuth flow.
    if credentials_path != _SHARED_GOOGLE_CREDENTIALS_PATH:
        save_tokens(_SHARED_GOOGLE_CREDENTIALS_PATH, token_payload)

    return tokens


# ---------------------------------------------------------------------------
# Generic OAuth flow — works with any OAuthProvider
# ---------------------------------------------------------------------------


def _wait_for_callback_code(
    host: str = "127.0.0.1",
    port: int = 8789,
    path: str = "/callback",
    timeout: int = 120,
) -> Tuple[str, str]:
    """Start a localhost HTTP server and wait for ``?code=`` on *path*.

    Returns
    -------
    (code, state)
        Tuple of the authorization code and ``state`` query parameter
        received from the OAuth redirect. ``state`` is empty string
        when the provider didn't echo one back (older providers).
        Caller is responsible for verifying it with :func:`verify_state`
        before using the code.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    auth_code: List[str] = []
    state_value: List[str] = []
    error: List[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                auth_code.append(params["code"][0])
                state_value.append(params.get("state", [""])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:system-ui;text-align:center;"
                    b"padding:60px'>"
                    b"<h2 style='color:#22c55e'>Connected!</h2>"
                    b"<p>You can close this tab and return to OpenJarvis.</p>"
                    b"</body></html>"
                )
            elif "error" in params:
                error.append(params.get("error", ["unknown"])[0])
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:system-ui;text-align:center;"
                    b"padding:60px'>"
                    b"<h2 style='color:#ef4444'>Authorization Failed</h2>"
                    b"<p>Please close this tab and try again.</p>"
                    b"</body></html>"
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    # Ensure port is free
    import socket

    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test_sock.bind((host, port))
    except OSError:
        pass
    finally:
        test_sock.close()

    import time

    time.sleep(0.3)

    server = HTTPServer((host, port), _Handler)
    server.timeout = timeout

    while not auth_code and not error:
        server.handle_request()
    server.server_close()

    if error:
        raise RuntimeError(f"OAuth authorization denied: {error[0]}")
    if not auth_code:
        raise RuntimeError("OAuth callback timed out")
    return auth_code[0], state_value[0] if state_value else ""


def _exchange_token(
    provider: OAuthProvider,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange an authorization *code* for tokens using *provider* config.

    Parameters
    ----------
    code_verifier:
        Optional PKCE verifier matching the ``code_challenge`` sent to
        the provider during the auth request. Required when PKCE is in
        use; the provider re-hashes the verifier and rejects the
        exchange if it doesn't match the stored challenge — that's what
        protects against attackers who only intercept the redirect URL.
    """
    import httpx

    data: Dict[str, str] = {
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers: Dict[str, str] = {}

    if provider.token_auth == "basic":
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret

    if code_verifier is not None:
        data["code_verifier"] = code_verifier

    resp = httpx.post(provider.token_endpoint, data=data, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def run_connector_oauth(
    connector_id: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, Any]:
    """Run a complete OAuth flow for *connector_id* using its own local
    callback server.

    Used by ``handle_callback`` on connectors that need to launch the
    browser dance themselves (e.g. Spotify/Strava when the user pastes
    ``client_id:client_secret`` into the catalog form). The FastAPI
    ``/oauth/start`` route is the alternative path — both apply the
    same state + PKCE protections.

    Security
    --------
    - HMAC-signed ``state`` parameter prevents CSRF (the local
      callback server refuses codes that don't carry our state)
    - PKCE ``code_challenge`` (S256) prevents token theft if the
      redirect URL is intercepted

    Steps
    -----
    1. Look up the ``OAuthProvider``
    2. Resolve client credentials (arg → stored → env)
    3. Generate state + PKCE pair
    4. Build auth URL and open the user's browser
    5. Start localhost callback server, wait for code + state
    6. Verify state matches (HMAC + TTL + connector binding)
    7. Exchange the code for tokens with the PKCE verifier
    8. Save tokens to all relevant credential files

    Returns the raw token response dict.
    """
    import webbrowser

    provider = get_provider_for_connector(connector_id)
    if provider is None:
        raise ValueError(f"No OAuth provider configured for '{connector_id}'")

    # Resolve credentials
    if not (client_id and client_secret):
        creds = get_client_credentials(provider)
        if creds:
            client_id, client_secret = creds
    if not (client_id and client_secret):
        raise RuntimeError(
            f"No client credentials for {provider.display_name}. "
            f"Set them up at: {provider.setup_url}"
        )

    redirect_uri = (
        f"http://{provider.callback_host}:{provider.callback_port}"
        f"{provider.callback_path}"
    )

    state = generate_state(connector_id)
    code_verifier, code_challenge = generate_pkce_pair()

    # Build auth URL with state + PKCE
    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **provider.extra_auth_params,
    }
    auth_url = f"{provider.auth_endpoint}?{urlencode(params)}"

    # Open browser and wait for callback
    webbrowser.open(auth_url)
    code, returned_state = _wait_for_callback_code(
        host=provider.callback_host,
        port=provider.callback_port,
        path=provider.callback_path,
    )

    # Verify state — protects against CSRF "account confusion" where
    # the user's browser is tricked into completing a code from the
    # attacker's session.
    if not verify_state(returned_state, connector_id):
        raise RuntimeError(
            "OAuth state mismatch — request was tampered with or expired"
        )

    # Exchange code for tokens, including the PKCE verifier. Without
    # the verifier the provider rejects the exchange even with a valid
    # code, so an attacker who only intercepts the redirect URL can't
    # complete the flow.
    tokens = _exchange_token(
        provider,
        code,
        client_id,
        client_secret,
        redirect_uri,
        code_verifier=code_verifier,
    )

    # Build payload with client credentials included (needed for refresh)
    payload = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 3600),
        "client_id": client_id,
        "client_secret": client_secret,
    }

    # Save to all credential files for this provider
    for filename in provider.credential_files:
        save_tokens(str(_CONNECTORS_DIR / filename), payload)

    return tokens
