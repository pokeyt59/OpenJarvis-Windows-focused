"""Tests for OAuth security primitives — state (CSRF) + PKCE.

These cover the helpers added in oauth.py:
- ``generate_state`` / ``verify_state``  — HMAC-signed CSRF tokens
- ``generate_pkce_pair`` / ``remember_pkce_verifier`` /
  ``consume_pkce_verifier`` — RFC 7636 PKCE pair, verifier kept in
  process memory only

Plus end-to-end checks that the hardened ``run_connector_oauth`` and
``run_oauth_flow`` paths pass the verifier through to the token
exchange (the actual security guarantee: provider rejects exchange
without matching verifier).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# state (CSRF) helpers
# ---------------------------------------------------------------------------


def test_state_round_trips_for_same_connector() -> None:
    from openjarvis.connectors.oauth import generate_state, verify_state

    state = generate_state("onenote")
    assert verify_state(state, "onenote") is True


def test_state_is_bound_to_connector_id() -> None:
    """State issued for connector A must not validate for connector B.

    Without this binding, an attacker could initiate /oauth/start for
    one connector and replay the resulting state at the callback for a
    different connector — an "account confusion" variant.
    """
    from openjarvis.connectors.oauth import generate_state, verify_state

    state = generate_state("onenote")
    assert verify_state(state, "gdrive") is False
    assert verify_state(state, "spotify") is False


def test_state_rejects_tampered_payload() -> None:
    """Changing any byte of the payload breaks the HMAC check."""
    from openjarvis.connectors.oauth import generate_state, verify_state

    state = generate_state("onenote")
    payload_b64, sig_b64 = state.split(".", 1)
    # Flip a character in the payload — should fail verification
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    assert verify_state(f"{tampered}.{sig_b64}", "onenote") is False


def test_state_rejects_forged_signature() -> None:
    """An attacker who doesn't know the signing key can't produce a valid sig."""
    from openjarvis.connectors.oauth import generate_state, verify_state

    state = generate_state("onenote")
    payload_b64, _ = state.split(".", 1)
    # Forge a plausible-looking signature
    assert verify_state(f"{payload_b64}.AAAAAAAAAAAAAAAA", "onenote") is False


def test_state_rejects_expired() -> None:
    """State older than the TTL is rejected, even if signature is valid.

    Uses ``time.time`` patch to simulate a state issued more than 10
    minutes ago (the configured TTL).
    """
    from openjarvis.connectors import oauth

    real_time = time.time()

    # Issue state at T=0
    with patch("openjarvis.connectors.oauth.time.time", return_value=real_time):
        state = oauth.generate_state("onenote")

    # Try to verify it at T=601 seconds later (TTL is 600s)
    with patch("openjarvis.connectors.oauth.time.time", return_value=real_time + 601):
        assert oauth.verify_state(state, "onenote") is False

    # But T=599 still works
    with patch("openjarvis.connectors.oauth.time.time", return_value=real_time + 599):
        assert oauth.verify_state(state, "onenote") is True


def test_state_rejects_malformed_input() -> None:
    """Empty / garbage / wrong-shape inputs return False, not raise."""
    from openjarvis.connectors.oauth import verify_state

    assert verify_state("", "onenote") is False
    assert verify_state("no-dot-here", "onenote") is False
    assert verify_state("!!!.!!!", "onenote") is False  # invalid base64
    assert verify_state("a.b.c", "onenote") is False  # too many parts (split keeps the c)


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def test_pkce_pair_has_correct_shape() -> None:
    """Verifier and challenge should both be base64url, no padding."""
    from openjarvis.connectors.oauth import generate_pkce_pair

    verifier, challenge = generate_pkce_pair()
    # base64url(32 bytes) = 43 chars after stripping padding
    assert len(verifier) == 43
    assert len(challenge) == 43
    # No padding characters
    assert "=" not in verifier
    assert "=" not in challenge
    # base64url alphabet
    import re

    assert re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", challenge)


def test_pkce_challenge_is_sha256_of_verifier() -> None:
    """Per RFC 7636 §4.2 — must be S256(verifier).

    If we don't compute the right transform, the provider rejects the
    exchange.
    """
    import base64
    import hashlib

    from openjarvis.connectors.oauth import generate_pkce_pair

    verifier, challenge = generate_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected


def test_pkce_pair_is_random_per_call() -> None:
    """Successive calls must produce different verifiers.

    If two flows shared a verifier, an attacker who intercepted one
    auth code could exchange it using the OTHER verifier.
    """
    from openjarvis.connectors.oauth import generate_pkce_pair

    v1, _ = generate_pkce_pair()
    v2, _ = generate_pkce_pair()
    assert v1 != v2


def test_pkce_verifier_round_trips_via_state_key() -> None:
    from openjarvis.connectors.oauth import (
        consume_pkce_verifier,
        remember_pkce_verifier,
    )

    remember_pkce_verifier("state-abc", "verifier-xyz")
    assert consume_pkce_verifier("state-abc") == "verifier-xyz"


def test_pkce_verifier_is_one_shot() -> None:
    """A state can only consume the verifier once. Replay attempts return None."""
    from openjarvis.connectors.oauth import (
        consume_pkce_verifier,
        remember_pkce_verifier,
    )

    remember_pkce_verifier("state-once", "v")
    assert consume_pkce_verifier("state-once") == "v"
    # Second consume must return None (the verifier was popped)
    assert consume_pkce_verifier("state-once") is None


def test_pkce_verifier_unknown_state_returns_none() -> None:
    from openjarvis.connectors.oauth import consume_pkce_verifier

    assert consume_pkce_verifier("never-stored") is None


def test_pkce_verifier_expires() -> None:
    """Stale entries (past TTL) get evicted on the next ``remember`` call."""
    from openjarvis.connectors import oauth

    real_time = time.time()
    with patch("openjarvis.connectors.oauth.time.time", return_value=real_time):
        oauth.remember_pkce_verifier("old", "verifier-old")

    # Advance past TTL (600s)
    with patch("openjarvis.connectors.oauth.time.time", return_value=real_time + 601):
        # Triggering remember on a new state opportunistically evicts "old"
        oauth.remember_pkce_verifier("new", "verifier-new")
        # The old one should be gone now
        assert oauth.consume_pkce_verifier("old") is None
        # The new one still works
        assert oauth.consume_pkce_verifier("new") == "verifier-new"


# ---------------------------------------------------------------------------
# Token exchange threads the verifier through
# ---------------------------------------------------------------------------


def test_exchange_token_includes_verifier_when_provided() -> None:
    """The PKCE verifier must travel to the provider on token exchange.

    Without this, the provider rejects with ``invalid_grant`` even if
    the code is valid — that's exactly what protects us when the code
    is intercepted from the redirect URL.
    """
    from openjarvis.connectors.oauth import OAUTH_PROVIDERS, _exchange_token

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "ya29.test"}
    mock_resp.raise_for_status = MagicMock()

    provider = OAUTH_PROVIDERS["strava"]  # token_auth="body" (not basic)
    with patch("httpx.post", return_value=mock_resp) as mock_post:
        _exchange_token(
            provider,
            code="auth-code",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://127.0.0.1:8789/callback",
            code_verifier="my-verifier",
        )

    posted = mock_post.call_args.kwargs["data"]
    assert posted["code_verifier"] == "my-verifier"
    assert posted["code"] == "auth-code"


def test_exchange_token_omits_verifier_when_none() -> None:
    """Backward compat: callers that don't pass a verifier still work."""
    from openjarvis.connectors.oauth import OAUTH_PROVIDERS, _exchange_token

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "ya29.test"}
    mock_resp.raise_for_status = MagicMock()

    provider = OAUTH_PROVIDERS["spotify"]  # token_auth="basic"
    with patch("httpx.post", return_value=mock_resp) as mock_post:
        _exchange_token(
            provider,
            code="auth-code",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://127.0.0.1:8888/callback",
        )

    posted = mock_post.call_args.kwargs["data"]
    assert "code_verifier" not in posted


def test_exchange_google_token_passes_verifier() -> None:
    """Same check for the legacy Google-specific exchange path."""
    from openjarvis.connectors.oauth import exchange_google_token

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "ya29"}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        exchange_google_token(
            code="4/test",
            client_id="cid",
            client_secret="cs",
            code_verifier="pkce-v",
        )

    posted = mock_post.call_args.kwargs["data"]
    assert posted["code_verifier"] == "pkce-v"


# ---------------------------------------------------------------------------
# Auth URL construction includes the right params
# ---------------------------------------------------------------------------


def test_build_google_auth_url_includes_state_and_challenge() -> None:
    """When state + code_challenge are provided, they appear in the URL."""
    from urllib.parse import parse_qs, urlparse

    from openjarvis.connectors.oauth import build_google_auth_url

    url = build_google_auth_url(
        client_id="abc",
        state="my-state",
        code_challenge="my-challenge",
    )
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["state"] == ["my-state"]
    assert qs["code_challenge"] == ["my-challenge"]
    assert qs["code_challenge_method"] == ["S256"]


def test_build_google_auth_url_omits_state_when_absent() -> None:
    """Backward compat — callers that don't pass state get a URL without it."""
    from urllib.parse import parse_qs, urlparse

    from openjarvis.connectors.oauth import build_google_auth_url

    url = build_google_auth_url(client_id="abc")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert "state" not in qs
    assert "code_challenge" not in qs
