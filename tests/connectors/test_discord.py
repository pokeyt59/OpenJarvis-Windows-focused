"""Tests for DiscordConnector — Discord DM sync.

All Discord REST calls mocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry


# ---------------------------------------------------------------------------
# Fake payloads
# ---------------------------------------------------------------------------


_DM_CHANNELS = [
    # Single-user DM
    {
        "id": "chan-1",
        "type": 1,
        "recipients": [{"id": "user-a", "username": "alice", "global_name": "Alice"}],
    },
    # Group DM with a name
    {
        "id": "chan-2",
        "type": 3,
        "name": "Project Aurora",
        "recipients": [
            {"id": "user-b", "username": "bob"},
            {"id": "user-c", "username": "carol"},
        ],
    },
    # Group DM without a name (should fall back to joined recipients)
    {
        "id": "chan-3",
        "type": 3,
        "recipients": [
            {"id": "user-d", "username": "dan"},
            {"id": "user-e", "username": "eve"},
        ],
    },
    # Unexpected channel type — must be skipped
    {"id": "chan-skip", "type": 99, "recipients": []},
]


def _msg(
    msg_id: str,
    content: str,
    author: str = "Sender",
    ts: str = "2024-04-05T10:00:00Z",
) -> dict:
    return {
        "id": msg_id,
        "content": content,
        "author": {"id": "u1", "username": author, "global_name": author},
        "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def connector(tmp_path: Path):
    from openjarvis.connectors.discord import DiscordConnector  # noqa: PLC0415

    creds_path = str(tmp_path / "discord.json")
    return DiscordConnector(credentials_path=creds_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_without_token(connector) -> None:
    assert connector.is_connected() is False


def test_connected_with_inline_token() -> None:
    from openjarvis.connectors.discord import DiscordConnector  # noqa: PLC0415

    conn = DiscordConnector(token="user.token.value")
    assert conn.is_connected() is True


def test_connected_with_stored_token(connector) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "user.token.value"}), encoding="utf-8"
    )
    assert connector.is_connected() is True


def test_auth_url(connector) -> None:
    assert "discord.com" in connector.auth_url()


def test_handle_callback_persists_token(connector) -> None:
    connector.handle_callback("  trim-me  ")
    stored = json.loads(Path(connector._credentials_path).read_text(encoding="utf-8"))
    assert stored["token"] == "trim-me"


@patch("openjarvis.connectors.discord._discord_api_list_messages")
@patch("openjarvis.connectors.discord._discord_api_list_dms")
def test_sync_yields_documents_per_message(
    mock_list_dms,
    mock_list_messages,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "token1"}), encoding="utf-8"
    )
    mock_list_dms.return_value = _DM_CHANNELS

    # 2 messages in chan-1, 1 in chan-2, 1 in chan-3, none from chan-skip.
    def _by_channel(token, channel_id, **kwargs):
        if kwargs.get("before"):
            return []  # Single page per channel for this test.
        if channel_id == "chan-1":
            return [_msg("m1", "Hi"), _msg("m2", "Hello again")]
        if channel_id == "chan-2":
            return [_msg("m3", "Standup in 10")]
        if channel_id == "chan-3":
            return [_msg("m4", "wyd")]
        return []

    mock_list_messages.side_effect = _by_channel

    docs: List[Document] = list(connector.sync())

    ids = {d.doc_id for d in docs}
    assert ids == {
        "discord:chan-1:m1",
        "discord:chan-1:m2",
        "discord:chan-2:m3",
        "discord:chan-3:m4",
    }


@patch("openjarvis.connectors.discord._discord_api_list_messages")
@patch("openjarvis.connectors.discord._discord_api_list_dms")
def test_sync_skips_unknown_channel_types(
    mock_list_dms,
    mock_list_messages,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "token1"}), encoding="utf-8"
    )
    mock_list_dms.return_value = _DM_CHANNELS
    mock_list_messages.return_value = []

    list(connector.sync())
    # _discord_api_list_messages was called for chan-1, chan-2, chan-3 but
    # NOT chan-skip (type 99).
    called_channels = {c.args[1] for c in mock_list_messages.call_args_list}
    assert "chan-skip" not in called_channels
    assert called_channels == {"chan-1", "chan-2", "chan-3"}


@patch("openjarvis.connectors.discord._discord_api_list_messages")
@patch("openjarvis.connectors.discord._discord_api_list_dms")
def test_sync_skips_empty_content(
    mock_list_dms,
    mock_list_messages,
    connector,
) -> None:
    """Messages with no text (pure attachments / system messages) are skipped."""
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "token1"}), encoding="utf-8"
    )
    mock_list_dms.return_value = [_DM_CHANNELS[0]]
    mock_list_messages.return_value = [
        _msg("m1", ""),
        _msg("m2", "text!"),
    ]

    docs = list(connector.sync())
    assert {d.doc_id for d in docs} == {"discord:chan-1:m2"}


@patch("openjarvis.connectors.discord._discord_api_list_messages")
@patch("openjarvis.connectors.discord._discord_api_list_dms")
def test_sync_stops_paging_past_since_cutoff(
    mock_list_dms,
    mock_list_messages,
    connector,
) -> None:
    """Once a message older than since shows up, further paging is skipped."""
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "token1"}), encoding="utf-8"
    )
    mock_list_dms.return_value = [_DM_CHANNELS[0]]

    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=30)
    mock_list_messages.return_value = [
        _msg("recent", "fresh", ts=recent.isoformat().replace("+00:00", "Z")),
        _msg("ancient", "stale", ts=old.isoformat().replace("+00:00", "Z")),
    ]

    cutoff = now - timedelta(days=7)
    docs = list(connector.sync(since=cutoff))
    ids = {d.doc_id for d in docs}
    assert ids == {"discord:chan-1:recent"}
    # Only one batch fetched — paging stops as soon as the ancient message
    # is encountered.
    assert mock_list_messages.call_count == 1


@patch("openjarvis.connectors.discord._discord_api_list_messages")
@patch("openjarvis.connectors.discord._discord_api_list_dms")
def test_sync_pagination_walks_back(
    mock_list_dms,
    mock_list_messages,
    connector,
) -> None:
    """The connector pages with ``before`` until a page comes back short."""
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "token1"}), encoding="utf-8"
    )
    mock_list_dms.return_value = [_DM_CHANNELS[0]]

    # First page: full 100; second page: 1 message → stop.
    full_page = [
        _msg(f"m{i}", f"msg {i}") for i in range(100)
    ]
    mock_list_messages.side_effect = [full_page, [_msg("tail", "the end")]]

    docs = list(connector.sync())
    assert len(docs) == 101
    # Second call passed ``before`` = id of the last message on page 1.
    second_kwargs = mock_list_messages.call_args_list[1].kwargs
    assert second_kwargs["before"] == "m99"


def test_channel_display_name_single_dm() -> None:
    from openjarvis.connectors.discord import _channel_display_name  # noqa: PLC0415

    name = _channel_display_name(_DM_CHANNELS[0])
    assert name == "Alice"


def test_channel_display_name_named_group_dm() -> None:
    from openjarvis.connectors.discord import _channel_display_name  # noqa: PLC0415

    name = _channel_display_name(_DM_CHANNELS[1])
    assert name == "Project Aurora"


def test_channel_display_name_unnamed_group_dm() -> None:
    from openjarvis.connectors.discord import _channel_display_name  # noqa: PLC0415

    name = _channel_display_name(_DM_CHANNELS[2])
    assert "dan" in name
    assert "eve" in name


def test_disconnect(connector) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"token": "x"}), encoding="utf-8"
    )
    connector.disconnect()
    assert not Path(connector._credentials_path).exists()
    assert connector.is_connected() is False


def test_registry() -> None:
    from openjarvis.connectors.discord import DiscordConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("discord", DiscordConnector)
    assert ConnectorRegistry.contains("discord")
    cls = ConnectorRegistry.get("discord")
    assert cls.connector_id == "discord"
