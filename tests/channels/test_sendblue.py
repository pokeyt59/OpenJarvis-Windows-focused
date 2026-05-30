"""Tests for SendBlue iMessage/SMS channel.

Covers: init, env-var fallback, connect, send (mocked httpx), webhook
handler, event emission, and registry registration.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.channels._stubs import ChannelStatus
from openjarvis.channels.sendblue import SendBlueChannel
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.registry import ChannelRegistry
from tests.channels.channel_test_helpers import make_common_channel_tests


@pytest.fixture(autouse=True)
def _register_sendblue():
    if not ChannelRegistry.contains("sendblue"):
        ChannelRegistry.register_value("sendblue", SendBlueChannel)


TestCommonChannel = make_common_channel_tests(
    SendBlueChannel,
    "sendblue",
    constructor_kwargs={
        "api_key_id": "test_key",
        "api_secret_key": "test_secret",
        "from_number": "+15551234567",
    },
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(**overrides):
    defaults = {
        "api_key_id": "test_key",
        "api_secret_key": "test_secret",
        "from_number": "+15551234567",
    }
    defaults.update(overrides)
    return SendBlueChannel(**defaults)


def _mock_httpx_post(status=200, body=None):
    """Return a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(body or {})
    resp.json.return_value = body or {}
    return resp


# ---------------------------------------------------------------------------
# Init / env vars
# ---------------------------------------------------------------------------


class TestInit:
    def test_from_params(self):
        ch = _make_channel()
        assert ch.channel_id == "sendblue"
        assert ch._api_key_id == "test_key"
        assert ch._api_secret_key == "test_secret"
        assert ch._from_number == "+15551234567"
        assert ch.status() == ChannelStatus.DISCONNECTED

    def test_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("SENDBLUE_API_KEY_ID", "env_key")
        monkeypatch.setenv("SENDBLUE_API_SECRET_KEY", "env_secret")
        monkeypatch.setenv("SENDBLUE_FROM_NUMBER", "+19998887777")

        ch = SendBlueChannel()
        assert ch._api_key_id == "env_key"
        assert ch._api_secret_key == "env_secret"
        assert ch._from_number == "+19998887777"

    def test_no_credentials(self):
        ch = SendBlueChannel()
        ch.connect()
        assert ch.status() == ChannelStatus.ERROR


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnect:
    def test_connect_with_creds(self):
        ch = _make_channel()
        ch.connect()
        assert ch.status() == ChannelStatus.CONNECTED

    def test_disconnect(self):
        ch = _make_channel()
        ch.connect()
        ch.disconnect()
        assert ch.status() == ChannelStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_success(self):
        ch = _make_channel()
        ch.connect()
        with patch("httpx.post", return_value=_mock_httpx_post(200)):
            result = ch.send("+19998887777", "Hello!")
        assert result is True

    def test_send_includes_from_number(self):
        ch = _make_channel(from_number="+15559876543")
        ch.connect()
        with patch("httpx.post", return_value=_mock_httpx_post(200)) as mock:
            ch.send("+19998887777", "Hi!")
            call_kwargs = mock.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert payload["number"] == "+19998887777"
            assert payload["content"] == "Hi!"
            assert payload["from_number"] == "+15559876543"

    def test_send_api_error_returns_false(self):
        ch = _make_channel()
        ch.connect()
        with patch("httpx.post", return_value=_mock_httpx_post(401)):
            result = ch.send("+19998887777", "Hello!")
        assert result is False

    def test_send_network_error_returns_false(self):
        ch = _make_channel()
        ch.connect()
        with patch("httpx.post", side_effect=Exception("Network error")):
            result = ch.send("+19998887777", "Hello!")
        assert result is False

    def test_send_no_credentials_returns_false(self):
        ch = SendBlueChannel()
        result = ch.send("+19998887777", "Hello!")
        assert result is False

    def test_send_publishes_event(self):
        bus = EventBus(record_history=True)
        ch = _make_channel(bus=bus)
        ch.connect()
        with patch("httpx.post", return_value=_mock_httpx_post(200)):
            ch.send("+19998887777", "Hello!")
        event_types = [e.event_type for e in bus.history]
        assert EventType.CHANNEL_MESSAGE_SENT in event_types


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


class TestWebhookHandler:
    def test_incoming_message_triggers_handlers(self):
        ch = _make_channel()
        received = []
        ch.on_message(lambda msg: received.append(msg))

        ch.handle_webhook(
            {
                "from_number": "+19127130720",
                "to_number": "+15551234567",
                "content": "Hello Jarvis",
                "message_handle": "msg-001",
                "is_outbound": False,
                "status": "RECEIVED",
                "service": "iMessage",
            }
        )

        assert len(received) == 1
        assert received[0].sender == "+19127130720"
        assert received[0].content == "Hello Jarvis"
        assert received[0].channel == "sendblue"

    def test_outbound_messages_ignored(self):
        ch = _make_channel()
        received = []
        ch.on_message(lambda msg: received.append(msg))

        ch.handle_webhook(
            {
                "from_number": "+15551234567",
                "content": "Outbound message",
                "is_outbound": True,
            }
        )

        assert len(received) == 0

    def test_empty_content_ignored(self):
        ch = _make_channel()
        received = []
        ch.on_message(lambda msg: received.append(msg))

        ch.handle_webhook(
            {
                "from_number": "+19127130720",
                "content": "",
                "is_outbound": False,
            }
        )

        assert len(received) == 0

    def test_incoming_publishes_event(self):
        bus = EventBus(record_history=True)
        ch = _make_channel(bus=bus)

        ch.handle_webhook(
            {
                "from_number": "+19127130720",
                "content": "Test",
                "message_handle": "msg-002",
                "is_outbound": False,
                "service": "iMessage",
            }
        )

        event_types = [e.event_type for e in bus.history]
        assert EventType.CHANNEL_MESSAGE_RECEIVED in event_types
        event = [
            e for e in bus.history if e.event_type == EventType.CHANNEL_MESSAGE_RECEIVED
        ][0]
        assert event.data["sender"] == "+19127130720"
        assert event.data["service"] == "iMessage"

    def test_handler_exception_does_not_crash(self):
        ch = _make_channel()
        ch.on_message(lambda msg: 1 / 0)  # Will raise ZeroDivisionError

        # Should not raise
        ch.handle_webhook(
            {
                "from_number": "+19127130720",
                "content": "Test",
                "message_handle": "msg-003",
                "is_outbound": False,
            }
        )


# ---------------------------------------------------------------------------
# mark_read / send_typing (BUG #1 -> FEATURE: native iMessage signals)
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_success_returns_true_and_targets_mark_read_endpoint(self):
        ch = _make_channel()
        with patch("httpx.post", return_value=_mock_httpx_post(200)) as mock:
            assert ch.mark_read("+19998887777") is True
        url = mock.call_args.args[0]
        assert url.endswith("/api/mark-read")
        # Host is the centralized API_BASE; do not hardcode .com here either.
        from openjarvis.channels.sendblue import API_BASE

        assert url.startswith(API_BASE)

    def test_includes_required_headers_and_from_number(self):
        ch = _make_channel(from_number="+15559876543")
        with patch("httpx.post", return_value=_mock_httpx_post(200)) as mock:
            ch.mark_read("+19998887777")
        kwargs = mock.call_args.kwargs
        assert kwargs["headers"]["sb-api-key-id"] == "test_key"
        assert kwargs["headers"]["sb-api-secret-key"] == "test_secret"
        # `from_number` is documented as REQUIRED for mark-read.
        assert kwargs["json"]["number"] == "+19998887777"
        assert kwargs["json"]["from_number"] == "+15559876543"

    def test_non_2xx_returns_false_non_fatal(self):
        ch = _make_channel()
        with patch("httpx.post", return_value=_mock_httpx_post(404)):
            assert ch.mark_read("+19998887777") is False

    def test_exception_returns_false_non_fatal(self):
        ch = _make_channel()
        with patch("httpx.post", side_effect=Exception("boom")):
            assert ch.mark_read("+19998887777") is False

    def test_no_credentials_returns_false(self):
        ch = SendBlueChannel()
        assert ch.mark_read("+19998887777") is False


class TestSendTyping:
    def test_success_returns_true_and_targets_typing_endpoint(self):
        ch = _make_channel()
        with patch("httpx.post", return_value=_mock_httpx_post(200)) as mock:
            assert ch.send_typing("+19998887777") is True
        url = mock.call_args.args[0]
        assert url.endswith("/api/send-typing-indicator")

    def test_includes_headers_and_payload(self):
        ch = _make_channel(from_number="+15559876543")
        with patch("httpx.post", return_value=_mock_httpx_post(200)) as mock:
            ch.send_typing("+19998887777")
        kwargs = mock.call_args.kwargs
        assert kwargs["headers"]["sb-api-key-id"] == "test_key"
        assert kwargs["json"]["number"] == "+19998887777"
        assert kwargs["json"]["from_number"] == "+15559876543"

    def test_non_2xx_returns_false_non_fatal(self):
        ch = _make_channel()
        with patch("httpx.post", return_value=_mock_httpx_post(403)):
            assert ch.send_typing("+19998887777") is False

    def test_exception_returns_false_non_fatal(self):
        ch = _make_channel()
        with patch("httpx.post", side_effect=Exception("boom")):
            assert ch.send_typing("+19998887777") is False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_from_number(self):
        ch = _make_channel(from_number="+15559876543")
        assert ch.from_number == "+15559876543"

    def test_signal_toggles_default_on(self):
        ch = _make_channel()
        assert ch.read_receipts_enabled is True
        assert ch.typing_indicator_enabled is True

    def test_signal_toggles_off(self):
        ch = _make_channel(read_receipts=False, typing_indicator=False)
        assert ch.read_receipts_enabled is False
        assert ch.typing_indicator_enabled is False
