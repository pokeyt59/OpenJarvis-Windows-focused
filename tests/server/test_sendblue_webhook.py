"""Integration tests for the SendBlue webhook endpoint.

Tests the /webhooks/sendblue route, health check endpoint, and the
full flow from incoming webhook -> bridge -> agent -> send response.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from openjarvis.core.registry import ChannelRegistry  # noqa: E402


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until True or timeout. Returns the final result.

    The /webhooks/sendblue handler launches its work via
    ``asyncio.create_task(asyncio.to_thread(_handle_and_reply))`` and returns
    200 immediately, so test assertions about channel calls have to wait for
    the background thread. Same pattern as ``tests/server/test_ws_bridge.py``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _register_sendblue():
    if not ChannelRegistry.contains("sendblue"):
        from openjarvis.channels.sendblue import SendBlueChannel

        ChannelRegistry.register_value("sendblue", SendBlueChannel)


@pytest.fixture
def mock_bridge():
    bridge = MagicMock()
    bridge.handle_incoming.return_value = "Here are your results..."
    return bridge


@pytest.fixture
def sendblue_channel():
    from openjarvis.channels.sendblue import SendBlueChannel

    ch = SendBlueChannel(
        api_key_id="test_key",
        api_secret_key="test_secret",
        from_number="+15551234567",
    )
    ch.connect()
    return ch


@pytest.fixture
def webhook_app(mock_bridge, sendblue_channel):
    from openjarvis.server.webhook_routes import create_webhook_router

    app = FastAPI()
    router = create_webhook_router(
        bridge=mock_bridge,
        sendblue_channel=sendblue_channel,
    )
    app.include_router(router)
    return app


@pytest.fixture
def client(webhook_app):
    return TestClient(webhook_app)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


class TestSendBlueWebhook:
    def test_incoming_message_returns_200(self, client):
        resp = client.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "to_number": "+15551234567",
                "content": "Hello Jarvis",
                "message_handle": "msg-001",
                "is_outbound": False,
                "status": "RECEIVED",
                "service": "iMessage",
            },
        )
        assert resp.status_code == 200

    def test_outbound_status_callback_ignored(self, client, mock_bridge):
        resp = client.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+15551234567",
                "content": "Sent message",
                "is_outbound": True,
            },
        )
        assert resp.status_code == 200
        mock_bridge.handle_incoming.assert_not_called()

    def test_empty_content_ignored(self, client, mock_bridge):
        resp = client.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "",
                "is_outbound": False,
            },
        )
        assert resp.status_code == 200
        mock_bridge.handle_incoming.assert_not_called()

    def test_missing_from_number_ignored(self, client, mock_bridge):
        resp = client.post(
            "/webhooks/sendblue",
            json={
                "content": "Hello",
                "is_outbound": False,
            },
        )
        assert resp.status_code == 200
        mock_bridge.handle_incoming.assert_not_called()

    def test_webhook_secret_validation(self, mock_bridge):
        """When a webhook secret is set, reject requests without it."""
        from openjarvis.channels.sendblue import SendBlueChannel
        from openjarvis.server.webhook_routes import create_webhook_router

        ch = SendBlueChannel(
            api_key_id="k",
            api_secret_key="s",
            from_number="+1555",
            webhook_secret="mysecret",
        )
        ch.connect()

        app = FastAPI()
        router = create_webhook_router(bridge=mock_bridge, sendblue_channel=ch)
        app.include_router(router)
        c = TestClient(app)

        # Without secret header -> rejected
        resp = c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hello",
                "is_outbound": False,
            },
        )
        assert resp.status_code == 403

        # With correct secret -> accepted
        resp = c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hello",
                "is_outbound": False,
                "message_handle": "msg-002",
            },
            headers={"x-sendblue-secret": "mysecret"},
        )
        assert resp.status_code == 200

    def test_no_bridge_returns_200(self, sendblue_channel):
        """When no bridge exists, webhook should not crash."""
        from openjarvis.server.webhook_routes import create_webhook_router

        app = FastAPI()
        router = create_webhook_router(bridge=None, sendblue_channel=sendblue_channel)
        app.include_router(router)
        c = TestClient(app)

        resp = c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hello",
                "is_outbound": False,
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Health endpoint (requires agent_manager_routes)
# ---------------------------------------------------------------------------


class TestSendBlueHealth:
    @pytest.fixture
    def health_app(self, sendblue_channel):
        app = FastAPI()
        app.state.sendblue_channel = sendblue_channel
        app.state.channel_bridge = MagicMock()
        app.state.channel_bridge._channels = {"sendblue": sendblue_channel}

        from openjarvis.server.agent_manager_routes import (
            create_agent_manager_router,
        )

        mgr = MagicMock()
        mgr.list_agents.return_value = []
        routers = create_agent_manager_router(mgr)
        sendblue_router = routers[4]  # 5th element is sendblue_router
        app.include_router(sendblue_router)
        return app

    def test_health_ready(self, health_app):
        c = TestClient(health_app)
        resp = c.get("/v1/channels/sendblue/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["channel_connected"] is True
        assert data["bridge_wired"] is True
        assert data["ready"] is True

    def test_health_not_ready(self):
        app = FastAPI()
        # No sendblue_channel or bridge on state

        from openjarvis.server.agent_manager_routes import (
            create_agent_manager_router,
        )

        mgr = MagicMock()
        mgr.list_agents.return_value = []
        routers = create_agent_manager_router(mgr)
        sendblue_router = routers[4]
        app.include_router(sendblue_router)

        c = TestClient(app)
        resp = c.get("/v1/channels/sendblue/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is False


# ---------------------------------------------------------------------------
# Webhook path: drift regression (BUG #1)
# ---------------------------------------------------------------------------


class TestSendBlueWebhookPath:
    """Regression guard for BUG #1.

    The frontend used to build ``/v1/channels/sendblue/webhook`` — a path the
    server never mounted — and silently 404'd every inbound delivery. The
    centralized ``WEBHOOK_PATH`` constant is now the single source of truth.
    If anyone moves the @router.post or renames the prefix without updating
    the constant, this test fails loudly.
    """

    def test_constant_matches_mounted_route(self, client):
        from openjarvis.channels.sendblue import WEBHOOK_PATH

        assert WEBHOOK_PATH == "/webhooks/sendblue"
        resp = client.post(
            WEBHOOK_PATH,
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "iMessage",
            },
        )
        # 200 — not 404 — means prefix + route compose to WEBHOOK_PATH.
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Native iMessage signals on inbound (FEATURE: read receipts + typing)
# ---------------------------------------------------------------------------


def _make_signal_app(mock_bridge, ch):
    """Helper: build a FastAPI app wired with the SendBlue channel + bridge."""
    from openjarvis.server.webhook_routes import create_webhook_router

    app = FastAPI()
    router = create_webhook_router(bridge=mock_bridge, sendblue_channel=ch)
    app.include_router(router)
    return app


class TestSendBlueInboundSignals:
    """Inbound handler fires mark_read + send_typing on iMessage, falls back
    to the text ack on SMS, and respects the read_receipts / typing_indicator
    toggles. (See FEATURES.md spec.)"""

    def _channel(self, mark_read_ok: bool = True, **kwargs):
        from openjarvis.channels.sendblue import SendBlueChannel

        ch = SendBlueChannel(
            api_key_id="k",
            api_secret_key="s",
            from_number="+15551234567",
            **kwargs,
        )
        ch.connect()
        # Replace the live HTTP calls with mocks.
        ch.mark_read = MagicMock(return_value=mark_read_ok)
        ch.send_typing = MagicMock(return_value=True)
        ch.send = MagicMock(return_value=True)
        return ch

    def test_imessage_triggers_mark_read_and_typing(self, mock_bridge):
        ch = self._channel(mark_read_ok=True)
        c = TestClient(_make_signal_app(mock_bridge, ch))
        resp = c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "iMessage",
            },
        )
        assert resp.status_code == 200
        assert _wait_for(lambda: ch.mark_read.called)
        assert _wait_for(lambda: ch.send_typing.called)
        ch.mark_read.assert_called_with("+19127130720")
        ch.send_typing.assert_called_with("+19127130720")

    def test_imessage_with_read_ok_skips_text_ack(self, mock_bridge):
        # When the native Read receipt lands, we suppress the legacy
        # "Message received! Working on it now..." text ack so the user just
        # sees the Read + typing bubble + reply.
        ch = self._channel(mark_read_ok=True)
        # Block the agent so the test observes the immediate-ack window only.
        mock_bridge.handle_incoming.side_effect = lambda *a, **k: time.sleep(5)
        c = TestClient(_make_signal_app(mock_bridge, ch))
        c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "iMessage",
            },
        )
        assert _wait_for(lambda: ch.mark_read.called)
        # Give the immediate-ack branch a moment to run if it were going to.
        time.sleep(0.1)
        # The only ``send`` calls should be the final reply (which the agent
        # is blocking on), so during the ack window send must not have run.
        assert ch.send.call_count == 0

    def test_imessage_with_read_fail_uses_text_ack(self, mock_bridge):
        ch = self._channel(mark_read_ok=False)
        c = TestClient(_make_signal_app(mock_bridge, ch))
        c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "iMessage",
            },
        )
        # Read receipt failed → text ack fallback kicks in.
        assert _wait_for(lambda: ch.send.called)

    def test_sms_skips_native_signals_and_uses_text_ack(self, mock_bridge):
        ch = self._channel(mark_read_ok=True)
        c = TestClient(_make_signal_app(mock_bridge, ch))
        c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "SMS",
            },
        )
        # Wait for the background thread to do *something* observable.
        assert _wait_for(lambda: ch.send.called)
        # mark_read / send_typing are iMessage-only — never called on SMS.
        assert ch.mark_read.call_count == 0
        assert ch.send_typing.call_count == 0

    def test_read_receipts_toggle_off_skips_mark_read(self, mock_bridge):
        ch = self._channel(mark_read_ok=True, read_receipts=False)
        c = TestClient(_make_signal_app(mock_bridge, ch))
        c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "iMessage",
            },
        )
        # send_typing still fires; mark_read does not.
        assert _wait_for(lambda: ch.send_typing.called)
        assert ch.mark_read.call_count == 0

    def test_typing_toggle_off_skips_send_typing(self, mock_bridge):
        ch = self._channel(mark_read_ok=True, typing_indicator=False)
        c = TestClient(_make_signal_app(mock_bridge, ch))
        c.post(
            "/webhooks/sendblue",
            json={
                "from_number": "+19127130720",
                "content": "Hi",
                "is_outbound": False,
                "service": "iMessage",
            },
        )
        assert _wait_for(lambda: ch.mark_read.called)
        assert ch.send_typing.call_count == 0


# ---------------------------------------------------------------------------
# Register-webhook: documented body shape + verify-by-list (BUG #1 / #3 / #2)
# ---------------------------------------------------------------------------


class TestSendBlueRegisterWebhook:
    @pytest.fixture
    def register_app(self):
        from openjarvis.server.agent_manager_routes import (
            create_agent_manager_router,
        )

        app = FastAPI()
        mgr = MagicMock()
        mgr.list_agents.return_value = []
        routers = create_agent_manager_router(mgr)
        sendblue_router = routers[4]
        app.include_router(sendblue_router)
        return app

    def test_posts_documented_body_and_verifies(self, register_app):
        # SendBlue's GET returns the new URL only AFTER the POST — simulate
        # the round-trip with a list that grows.
        state = {"receive": []}

        def _fake_get(url, **_kwargs):
            assert url.endswith("/api/account/webhooks")
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"webhooks": {"receive": list(state["receive"])}}
            r.text = ""
            return r

        def _fake_post(url, **kwargs):
            assert url.endswith("/api/account/webhooks")
            # BUG #3: documented body shape.
            assert kwargs["json"] == {
                "webhooks": ["https://abc.ngrok.app/webhooks/sendblue"],
                "type": "receive",
            }
            state["receive"].append("https://abc.ngrok.app/webhooks/sendblue")
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"status": "OK"}
            r.text = ""
            return r

        with patch("httpx.get", side_effect=_fake_get), patch(
            "httpx.post", side_effect=_fake_post
        ):
            c = TestClient(register_app)
            resp = c.post(
                "/v1/channels/sendblue/register-webhook",
                json={
                    "api_key_id": "k",
                    "api_secret_key": "s",
                    "webhook_url": "https://abc.ngrok.app/webhooks/sendblue",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["registered"] is True
        assert data["verified"] is True

    def test_surfaces_stale_v1_webhook_entries(self, register_app):
        stale = "https://abc.ngrok.app/v1/channels/sendblue/webhook"
        new = "https://abc.ngrok.app/webhooks/sendblue"

        def _fake_get(_url, **_kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"webhooks": {"receive": [stale, new]}}
            r.text = ""
            return r

        def _fake_post(_url, **_kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"status": "OK"}
            r.text = ""
            return r

        with patch("httpx.get", side_effect=_fake_get), patch(
            "httpx.post", side_effect=_fake_post
        ):
            c = TestClient(register_app)
            resp = c.post(
                "/v1/channels/sendblue/register-webhook",
                json={
                    "api_key_id": "k",
                    "api_secret_key": "s",
                    "webhook_url": new,
                },
            )
        data = resp.json()
        assert data["verified"] is True
        assert stale in data["stale_webhooks"]

    def test_already_registered_skips_post(self, register_app):
        url = "https://abc.ngrok.app/webhooks/sendblue"

        def _fake_get(_u, **_k):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"webhooks": {"receive": [url]}}
            r.text = ""
            return r

        post_mock = MagicMock()
        with patch("httpx.get", side_effect=_fake_get), patch(
            "httpx.post", side_effect=post_mock
        ):
            c = TestClient(register_app)
            resp = c.post(
                "/v1/channels/sendblue/register-webhook",
                json={
                    "api_key_id": "k",
                    "api_secret_key": "s",
                    "webhook_url": url,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is True
        # Already-present URL: no POST issued, just verified.
        assert post_mock.call_count == 0


# ---------------------------------------------------------------------------
# BUG #4: webhook secret header is unverified (skipped TODO test)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "BUG #4: SendBlue's actual webhook-secret delivery scheme is "
        "unverified. The code asserts a plaintext echo in the "
        "`x-sendblue-secret` header, but SendBlue may sign the body via "
        "HMAC instead. When a real signed delivery is captured, replace "
        "this skip with an assertion that verifies hmac.compare_digest "
        "over the request body."
    )
)
class TestSendBlueWebhookSecretHMAC:
    def test_hmac_signature_is_verified_over_body(self):
        # Placeholder. See webhook_routes.py BUG #4 TODO.
        raise AssertionError("BUG #4 unresolved — header scheme unconfirmed")
