"""Discord connector — DM history via the official REST API.

Indexes only the signed-in user's **direct-message** channels. Guild channels
are deliberately skipped in this first release — most servers are too noisy
to be useful in a personal knowledge base, and pulling them requires the
``Read Message History`` permission per channel which is a separate UX
problem.

Authentication uses a Discord user token pasted into the UI. The token goes
in the ``Authorization`` header *raw* (no ``Bearer`` prefix) — that's the
shape Discord's API expects for user tokens, the same way bot tokens get a
``Bot `` prefix.

All HTTP calls are isolated in module-level helpers so tests can patch them
without exercising the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import httpx

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.connectors.oauth import delete_tokens, load_tokens, save_tokens
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CREDENTIALS_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "discord.json")
_DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord caps `limit` at 100 per request. We always ask for the max — the
# pagination cursor (`before`) lets us walk further back as needed.
_PER_REQUEST_LIMIT = 100

# Cap how far back we walk per channel in a single sync to bound the work.
# 500 = the most recent 5 page-fulls per DM. Plenty for a personal corpus.
_MAX_MESSAGES_PER_CHANNEL = 500


# ---------------------------------------------------------------------------
# Module-level API helpers (mockable in tests)
# ---------------------------------------------------------------------------


def _discord_headers(token: str) -> Dict[str, str]:
    """Headers for an authenticated request.

    User tokens use the bare token; bot tokens would be ``Bot <token>``. We
    only support user tokens here.
    """
    return {
        "Authorization": token,
        "Content-Type": "application/json",
    }


def _discord_api_list_dms(token: str) -> List[Dict[str, Any]]:
    """List the signed-in user's DM channels."""
    resp = httpx.get(
        f"{_DISCORD_API_BASE}/users/@me/channels",
        headers=_discord_headers(token),
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _discord_api_list_messages(
    token: str,
    channel_id: str,
    *,
    before: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch messages for *channel_id*, optionally before message ID *before*.

    Messages are returned newest-first.
    """
    params: Dict[str, Any] = {"limit": _PER_REQUEST_LIMIT}
    if before:
        params["before"] = before
    resp = httpx.get(
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers=_discord_headers(token),
        params=params,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_datetime(dt_str: str) -> datetime:
    if not dt_str:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(tz=timezone.utc)


def _channel_display_name(channel: Dict[str, Any]) -> str:
    """Render a DM channel's human-readable name.

    Discord channel types:
    * ``1`` — single-user DM. The recipient sits in ``recipients[0]``.
    * ``3`` — group DM. May have a ``name`` field; if not, join recipients.

    Anything else is unexpected for ``/users/@me/channels`` — fall back to
    the channel id.
    """
    channel_type = channel.get("type")
    recipients: List[Dict[str, Any]] = channel.get("recipients", []) or []

    if channel_type == 1 and recipients:
        r = recipients[0]
        return (
            r.get("global_name")
            or r.get("username")
            or r.get("id", "")
            or "(unknown user)"
        )
    if channel_type == 3:
        name = channel.get("name", "")
        if name:
            return name
        if recipients:
            names = [
                r.get("global_name") or r.get("username") or r.get("id", "")
                for r in recipients
            ]
            return ", ".join(filter(None, names)) or "(group DM)"
        return "(group DM)"
    return channel.get("id", "(unknown channel)")


def _author_display_name(message: Dict[str, Any]) -> str:
    """Pull the author's display name from a message payload."""
    author = message.get("author", {}) or {}
    return (
        author.get("global_name")
        or author.get("username")
        or author.get("id", "")
        or "Unknown"
    )


# ---------------------------------------------------------------------------
# DiscordConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("discord")
class DiscordConnector(BaseConnector):
    """Index direct messages from Discord via the REST API.

    Parameters
    ----------
    token:
        Discord user token. If provided, wins over any stored credentials
        file. Mostly useful for tests.
    credentials_path:
        Where to read/write the token from. Defaults to
        ``~/.openjarvis/connectors/discord.json``.
    """

    connector_id = "discord"
    display_name = "Discord"
    auth_type = "oauth"  # token-based, same shape as Notion/Slack

    def __init__(
        self,
        token: str = "",
        credentials_path: str = "",
    ) -> None:
        self._token: str = token
        self._credentials_path: str = credentials_path or _DEFAULT_CREDENTIALS_PATH
        self._items_synced: int = 0
        self._items_total: int = 0
        self._last_sync: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Internal token resolution
    # ------------------------------------------------------------------

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        tokens = load_tokens(self._credentials_path)
        if tokens:
            return tokens.get("token", "")
        return ""

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        return bool(self._resolve_token())

    def disconnect(self) -> None:
        self._token = ""
        delete_tokens(self._credentials_path)

    def auth_url(self) -> str:
        """Return the URL where users find their token (developer docs)."""
        return "https://discord.com/developers/applications"

    def handle_callback(self, code: str) -> None:
        """Persist the pasted user token to the credentials file."""
        save_tokens(self._credentials_path, {"token": code.strip()})

    def sync(
        self,
        *,
        since: Optional[datetime] = None,
        cursor: Optional[str] = None,  # noqa: ARG002 — per-channel pagination is internal
    ) -> Iterator[Document]:
        """Yield :class:`Document` per Discord DM message across every channel."""
        token = self._resolve_token()
        if not token:
            return

        try:
            channels = _discord_api_list_dms(token)
        except httpx.HTTPError:
            return

        synced = 0

        for channel in channels:
            channel_id: str = channel.get("id", "")
            if not channel_id:
                continue
            channel_name = _channel_display_name(channel)
            channel_type = channel.get("type")
            # Channel type 1=DM, 3=group DM. Skip everything else just in
            # case Discord adds a new variant we don't recognise.
            if channel_type not in (1, 3):
                continue

            before: Optional[str] = None
            seen = 0
            stop_paging = False

            while seen < _MAX_MESSAGES_PER_CHANNEL and not stop_paging:
                try:
                    messages = _discord_api_list_messages(
                        token, channel_id, before=before
                    )
                except httpx.HTTPError:
                    break

                if not messages:
                    break

                for msg in messages:
                    msg_id: str = msg.get("id", "")
                    if not msg_id:
                        continue
                    content: str = msg.get("content", "") or ""
                    if not content:
                        # Skip pure-attachment / system messages — they
                        # have no searchable text.
                        continue

                    ts_str: str = msg.get("timestamp", "")
                    timestamp = _parse_iso_datetime(ts_str)
                    if since is not None:
                        since_aware = since
                        if since.tzinfo is None and timestamp.tzinfo is not None:
                            since_aware = since.replace(tzinfo=timezone.utc)
                        if timestamp < since_aware:
                            # Messages are newest-first; once we cross the
                            # since cutoff there's nothing older worth
                            # fetching for this channel.
                            stop_paging = True
                            break

                    author_name = _author_display_name(msg)

                    yield Document(
                        doc_id=f"discord:{channel_id}:{msg_id}",
                        source="discord",
                        doc_type="message",
                        content=content,
                        title=f"{author_name} in {channel_name}",
                        author=author_name,
                        timestamp=timestamp,
                        channel=channel_name,
                        thread_id=channel_id,
                        metadata={
                            "channel_id": channel_id,
                            "message_id": msg_id,
                            "channel_type": channel_type,
                        },
                    )
                    synced += 1

                # Page back further: `before` = the oldest message id in
                # this batch (Discord returns newest-first).
                if messages and not stop_paging:
                    before = messages[-1].get("id")
                    seen += len(messages)
                    if len(messages) < _PER_REQUEST_LIMIT:
                        break  # Last page for this channel.

        self._items_synced = synced
        self._last_sync = datetime.now(tz=timezone.utc)

    def sync_status(self) -> SyncStatus:
        return SyncStatus(
            state="idle",
            items_synced=self._items_synced,
            last_sync=self._last_sync,
        )
