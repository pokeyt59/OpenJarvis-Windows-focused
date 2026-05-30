"""SendBlue channel — iMessage/SMS API adapter.

Sends and receives iMessages (blue bubbles!) and SMS via the SendBlue API.
The agent gets a dedicated phone number; users text that number to interact.

API reference: https://docs.sendblue.com/api-v2/
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from openjarvis.channels._stubs import (
    BaseChannel,
    ChannelHandler,
    ChannelMessage,
    ChannelStatus,
)
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.registry import ChannelRegistry

logger = logging.getLogger(__name__)

# Single source of truth for SendBlue host + inbound webhook path.
# These are imported by openjarvis.server.{webhook_routes,agent_manager_routes}
# and any other module that constructs a SendBlue URL. Do not duplicate the
# literal "https://api.sendblue.co" or "/webhooks/sendblue" elsewhere.
#
# NOTE: This account is on `api.sendblue.co` (verified in the SendBlue
# dashboard playground). Do not switch to `.com` without confirming the host
# actually changed for this account.
API_BASE = "https://api.sendblue.co"
WEBHOOK_PATH = "/webhooks/sendblue"

# Back-compat alias for any external imports of the old private name.
_API_BASE = API_BASE


@ChannelRegistry.register("sendblue")
class SendBlueChannel(BaseChannel):
    """SendBlue iMessage/SMS channel adapter.

    Parameters
    ----------
    api_key_id:
        SendBlue API key ID.  Falls back to ``SENDBLUE_API_KEY_ID`` env var.
    api_secret_key:
        SendBlue API secret key.  Falls back to ``SENDBLUE_API_SECRET_KEY``
        env var.
    from_number:
        The SendBlue phone number to send from (E.164 format).
        Falls back to ``SENDBLUE_FROM_NUMBER`` env var.
    webhook_secret:
        Optional secret for verifying incoming webhook requests.
    bus:
        Optional event bus for publishing channel events.
    """

    channel_id = "sendblue"

    def __init__(
        self,
        *,
        api_key_id: str = "",
        api_secret_key: str = "",
        from_number: str = "",
        webhook_secret: str = "",
        read_receipts: bool = True,
        typing_indicator: bool = True,
        bus: Optional[EventBus] = None,
    ) -> None:
        self._api_key_id = api_key_id or os.environ.get("SENDBLUE_API_KEY_ID", "")
        self._api_secret_key = api_secret_key or os.environ.get(
            "SENDBLUE_API_SECRET_KEY", ""
        )
        self._from_number = from_number or os.environ.get("SENDBLUE_FROM_NUMBER", "")
        self._webhook_secret = webhook_secret
        # Native iMessage signal toggles. Set from the binding config dict
        # (`read_receipts` / `typing_indicator`, default True). The inbound
        # handler reads these off the channel instance and skips the
        # corresponding SendBlue API calls when False.
        self._read_receipts = bool(read_receipts)
        self._typing_indicator = bool(typing_indicator)
        self._bus = bus
        self._handlers: List[ChannelHandler] = []
        self._status = ChannelStatus.DISCONNECTED

    # -- connection lifecycle ---------------------------------------------------

    def connect(self) -> None:
        """Validate credentials and mark as connected."""
        if not self._api_key_id or not self._api_secret_key:
            logger.warning("No SendBlue API credentials configured")
            self._status = ChannelStatus.ERROR
            return
        self._status = ChannelStatus.CONNECTED

    def disconnect(self) -> None:
        """Mark as disconnected."""
        self._status = ChannelStatus.DISCONNECTED

    # -- send / receive --------------------------------------------------------

    def send(
        self,
        channel: str,
        content: str,
        *,
        conversation_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        """Send an iMessage/SMS via SendBlue.

        Parameters
        ----------
        channel:
            Recipient phone number in E.164 format (e.g. "+15551234567").
        content:
            Message text to send.
        """
        if not self._api_key_id or not self._api_secret_key:
            logger.warning("Cannot send: no SendBlue credentials configured")
            return False

        try:
            import httpx

            payload: Dict[str, Any] = {
                "number": channel,
                "content": content,
            }
            if self._from_number:
                payload["from_number"] = self._from_number

            resp = httpx.post(
                f"{API_BASE}/api/send-message",
                headers={
                    "sb-api-key-id": self._api_key_id,
                    "sb-api-secret-key": self._api_secret_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            if resp.status_code < 300:
                self._publish_sent(channel, content, conversation_id)
                return True
            logger.warning(
                "SendBlue API returned status %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        except Exception:
            logger.debug("SendBlue send failed", exc_info=True)
            return False

    # -- native iMessage signals (read receipt + typing) -----------------------

    def mark_read(self, number: str) -> bool:
        """Send a read receipt for the conversation with ``number``.

        iMessage / RCS only; non-2xx on SMS. SendBlue may require explicit
        account activation for read receipts. Best-effort: returns False on
        any failure without raising.
        """
        if not self._api_key_id or not self._api_secret_key:
            return False
        try:
            import httpx

            payload: Dict[str, Any] = {"number": number}
            if self._from_number:
                # `from_number` is documented as required for mark-read.
                payload["from_number"] = self._from_number
            resp = httpx.post(
                f"{API_BASE}/api/mark-read",
                headers={
                    "sb-api-key-id": self._api_key_id,
                    "sb-api-secret-key": self._api_secret_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15.0,
            )
            if resp.status_code >= 300:
                logger.debug(
                    "SendBlue mark-read returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            return resp.status_code < 300
        except Exception:
            logger.debug("SendBlue mark-read failed", exc_info=True)
            return False

    def send_typing(self, number: str) -> bool:
        """Show the "…" typing indicator to ``number``.

        iMessage only; requires an existing conversation (always true on
        inbound). The bubble fades after a few seconds, so callers that want
        to keep it alive must re-emit on a short interval. Best-effort.
        """
        if not self._api_key_id or not self._api_secret_key:
            return False
        try:
            import httpx

            payload: Dict[str, Any] = {"number": number}
            if self._from_number:
                payload["from_number"] = self._from_number
            resp = httpx.post(
                f"{API_BASE}/api/send-typing-indicator",
                headers={
                    "sb-api-key-id": self._api_key_id,
                    "sb-api-secret-key": self._api_secret_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15.0,
            )
            if resp.status_code >= 300:
                logger.debug(
                    "SendBlue typing-indicator returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            return resp.status_code < 300
        except Exception:
            logger.debug("SendBlue typing-indicator failed", exc_info=True)
            return False

    def handle_webhook(self, payload: Dict[str, Any]) -> None:
        """Process an incoming webhook payload from SendBlue.

        Expected fields: from_number, content, to_number, message_handle,
        is_outbound, status, service.
        """
        if payload.get("is_outbound", False):
            return  # Ignore outbound status callbacks

        from_number = payload.get("from_number", "")
        content = payload.get("content", "")
        message_handle = payload.get("message_handle", "")

        if not from_number or not content:
            return

        msg = ChannelMessage(
            channel="sendblue",
            sender=from_number,
            content=content,
            message_id=message_handle,
        )

        for handler in self._handlers:
            try:
                handler(msg)
            except Exception:
                logger.exception("Handler failed for SendBlue message")

        if self._bus is not None:
            self._bus.publish(
                EventType.CHANNEL_MESSAGE_RECEIVED,
                {
                    "channel": "sendblue",
                    "sender": from_number,
                    "content": content,
                    "message_id": message_handle,
                    "service": payload.get("service", ""),
                },
            )

    def status(self) -> ChannelStatus:
        """Return the current connection status."""
        return self._status

    def list_channels(self) -> List[str]:
        """Return available channel identifiers."""
        return ["sendblue"]

    def on_message(self, handler: ChannelHandler) -> None:
        """Register a callback for incoming messages."""
        self._handlers.append(handler)

    # -- properties ------------------------------------------------------------

    @property
    def from_number(self) -> str:
        """The SendBlue phone number this channel sends from."""
        return self._from_number

    @property
    def api_key_id(self) -> str:
        return self._api_key_id

    @property
    def api_secret_key(self) -> str:
        return self._api_secret_key

    @property
    def webhook_secret(self) -> str:
        return self._webhook_secret

    @property
    def read_receipts_enabled(self) -> bool:
        return self._read_receipts

    @property
    def typing_indicator_enabled(self) -> bool:
        return self._typing_indicator

    # -- internal helpers -------------------------------------------------------

    def _publish_sent(self, channel: str, content: str, conversation_id: str) -> None:
        """Publish a CHANNEL_MESSAGE_SENT event on the bus."""
        if self._bus is not None:
            self._bus.publish(
                EventType.CHANNEL_MESSAGE_SENT,
                {
                    "channel": "sendblue",
                    "recipient": channel,
                    "content": content,
                    "conversation_id": conversation_id,
                },
            )


__all__ = ["SendBlueChannel", "API_BASE", "WEBHOOK_PATH"]
