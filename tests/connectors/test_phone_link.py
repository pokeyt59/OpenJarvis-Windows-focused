"""Tests for PhoneLinkConnector — Phone Link SMS cache reader.

Builds fake SQLite DBs in tmp_path with shapes resembling the various
Phone Link schemas the connector tries to handle defensively.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages_db(path: Path) -> None:
    """A 'classic' Phone Link schema: messages table with body/timestamp/sender."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            body TEXT,
            received_timestamp INTEGER,
            sender TEXT,
            thread_id INTEGER
        )
        """
    )
    now = datetime.now(timezone.utc)
    rows = [
        (1, "Are you home?", int((now - timedelta(minutes=10)).timestamp() * 1000), "+15551234567", 1),
        (2, "Yes, on my way", int((now - timedelta(minutes=5)).timestamp() * 1000), "me", 1),
        (3, "Pick up bread?", int((now - timedelta(days=2)).timestamp() * 1000), "+15551234567", 1),
        (4, "", int(now.timestamp() * 1000), "+15551234567", 1),  # empty body — skipped
    ]
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _make_conversations_db(path: Path) -> None:
    """A 'newer' shape: Conversations table, text column instead of body."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE Conversations (
            message_id INTEGER PRIMARY KEY,
            text TEXT,
            sent_timestamp INTEGER,
            from_address TEXT
        )
        """
    )
    now = datetime.now(timezone.utc)
    rows = [
        (10, "Meeting at 3", int((now - timedelta(hours=2)).timestamp()), "+19998887777"),
        (11, "Got it", int((now - timedelta(hours=1)).timestamp()), "me"),
    ]
    conn.executemany("INSERT INTO Conversations VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _make_unrelated_db(path: Path) -> None:
    """A DB with no message-like tables — connector should ignore it."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT, value TEXT)")
    conn.execute("INSERT INTO settings VALUES ('theme', 'dark')")
    conn.commit()
    conn.close()


def _make_message_table_no_body(path: Path) -> None:
    """A message-named table that has no body-like column — connector skips it."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            sender TEXT,
            timestamp INTEGER
        )
        """
    )
    conn.execute("INSERT INTO messages VALUES (1, 'someone', 1234)")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_dir(tmp_path: Path) -> Path:
    """Build a fake Phone Link LocalState directory with a mix of DBs."""
    _make_messages_db(tmp_path / "messages.db")
    _make_conversations_db(tmp_path / "phone.db")
    _make_unrelated_db(tmp_path / "settings.db")
    _make_message_table_no_body(tmp_path / "shapeless.db")
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_when_dir_missing(tmp_path: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(tmp_path / "absent"))
    assert conn.is_connected() is False


def test_not_connected_when_dir_has_no_db(tmp_path: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(tmp_path))
    assert conn.is_connected() is False


def test_connected_when_db_present(populated_dir: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    assert conn.is_connected() is True


def test_sync_yields_from_classic_schema(populated_dir: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    docs: List[Document] = list(conn.sync())

    bodies = {d.content for d in docs}
    # Classic-schema rows present (empty body skipped).
    assert "Are you home?" in bodies
    assert "Yes, on my way" in bodies
    assert "Pick up bread?" in bodies


def test_sync_yields_from_alternate_schema(populated_dir: Path) -> None:
    """The newer Conversations / text / sent_timestamp / from_address shape works."""
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    docs = list(conn.sync())
    bodies = {d.content for d in docs}
    assert "Meeting at 3" in bodies
    assert "Got it" in bodies


def test_sync_skips_unrelated_databases(populated_dir: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    docs = list(conn.sync())
    # The settings.db has no message-like tables — its rows must not appear.
    for d in docs:
        assert d.metadata.get("db_name") != "settings.db"


def test_sync_skips_message_table_without_body(populated_dir: Path) -> None:
    """A 'messages' table with no body column is gracefully skipped."""
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    docs = list(conn.sync())
    # The shapeless.db should yield nothing.
    for d in docs:
        assert d.metadata.get("db_name") != "shapeless.db"


def test_doc_metadata_includes_provenance(populated_dir: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    docs = list(conn.sync())
    d = next(d for d in docs if d.content == "Meeting at 3")
    assert d.source == "phone_link"
    assert d.doc_type == "message"
    assert d.metadata["db_name"] == "phone.db"
    assert d.metadata["table"] == "Conversations"
    assert d.author == "+19998887777"


def test_sync_respects_since(populated_dir: Path) -> None:
    """Items older than the cutoff are filtered out."""
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    docs = list(conn.sync(since=cutoff))
    bodies = {d.content for d in docs}
    # 2-days-ago message is filtered out, recent ones stay.
    assert "Pick up bread?" not in bodies
    assert "Are you home?" in bodies


def test_normalise_timestamp_handles_multiple_shapes() -> None:
    from openjarvis.connectors.phone_link import _normalise_timestamp  # noqa: PLC0415

    # Epoch seconds (2024-04-05).
    sec = _normalise_timestamp(1_712_310_000)
    assert sec.year == 2024
    # Epoch milliseconds.
    ms = _normalise_timestamp(1_712_310_000_000)
    assert ms.year == 2024
    # Webkit microseconds since 1601-01-01.
    webkit_2024 = int(
        (datetime(2024, 4, 5, tzinfo=timezone.utc)
         - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds()
        * 1_000_000
    )
    wt = _normalise_timestamp(webkit_2024)
    assert wt.year == 2024
    # ISO string.
    iso = _normalise_timestamp("2024-04-05T10:00:00Z")
    assert iso.year == 2024


def test_disconnect(populated_dir: Path) -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    conn = PhoneLinkConnector(data_dir=str(populated_dir))
    assert conn.is_connected() is True
    conn.disconnect()
    assert conn.is_connected() is False


def test_registry() -> None:
    from openjarvis.connectors.phone_link import PhoneLinkConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("phone_link", PhoneLinkConnector)
    assert ConnectorRegistry.contains("phone_link")
    cls = ConnectorRegistry.get("phone_link")
    assert cls.connector_id == "phone_link"
