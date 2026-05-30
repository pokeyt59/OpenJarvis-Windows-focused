"""Tests for EdgeConnector — Microsoft Edge history + bookmarks reader.

The connector operates on local files; tests construct a fake profile dir
in tmp_path so no real Edge install is required.
"""

from __future__ import annotations

import json
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


_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _datetime_to_webkit(dt: datetime) -> int:
    """Convert a tz-aware datetime to Webkit microseconds since 1601-01-01."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _WEBKIT_EPOCH
    return int(delta.total_seconds() * 1_000_000)


def _make_history_db(path: Path, rows: List[dict]) -> None:
    """Create a minimal Edge-style History SQLite DB at *path*."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE urls (
            id INTEGER PRIMARY KEY,
            url LONGVARCHAR,
            title LONGVARCHAR,
            visit_count INTEGER DEFAULT 0,
            typed_count INTEGER DEFAULT 0,
            last_visit_time INTEGER,
            hidden INTEGER DEFAULT 0
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO urls (id, url, title, visit_count, typed_count, "
            "last_visit_time, hidden) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row["url"],
                row["title"],
                row.get("visit_count", 1),
                row.get("typed_count", 0),
                row["last_visit_time"],
                row.get("hidden", 0),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def edge_profile(tmp_path: Path) -> Path:
    """Build a fake Edge profile dir with History and Bookmarks files."""
    profile = tmp_path / "EdgeProfile"
    profile.mkdir()

    now = datetime.now(timezone.utc)
    _make_history_db(
        profile / "History",
        [
            {
                "id": 1,
                "url": "https://news.ycombinator.com",
                "title": "Hacker News",
                "visit_count": 10,
                "last_visit_time": _datetime_to_webkit(now - timedelta(hours=1)),
            },
            {
                "id": 2,
                "url": "https://github.com/microsoft/edge",
                "title": "Edge repo",
                "visit_count": 3,
                "last_visit_time": _datetime_to_webkit(now - timedelta(days=2)),
            },
            {
                "id": 3,
                "url": "https://example.com/hidden",
                "title": "Hidden",
                "visit_count": 1,
                "last_visit_time": _datetime_to_webkit(now),
                "hidden": 1,
            },
        ],
    )

    bookmarks = {
        "roots": {
            "bookmark_bar": {
                "type": "folder",
                "name": "Bookmarks bar",
                "children": [
                    {
                        "type": "url",
                        "name": "Anthropic",
                        "url": "https://anthropic.com",
                        "date_added": str(
                            _datetime_to_webkit(now - timedelta(days=10))
                        ),
                    },
                    {
                        "type": "folder",
                        "name": "Dev",
                        "children": [
                            {
                                "type": "url",
                                "name": "GitHub",
                                "url": "https://github.com",
                                "date_added": str(
                                    _datetime_to_webkit(now - timedelta(days=5))
                                ),
                            }
                        ],
                    },
                ],
            },
            "other": {
                "type": "folder",
                "name": "Other",
                "children": [
                    {
                        "type": "url",
                        "name": "Wikipedia",
                        "url": "https://wikipedia.org",
                        "date_added": str(
                            _datetime_to_webkit(now - timedelta(days=30))
                        ),
                    }
                ],
            },
        }
    }
    (profile / "Bookmarks").write_text(json.dumps(bookmarks), encoding="utf-8")

    return profile


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_without_profile(tmp_path: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(tmp_path / "nope"))
    assert conn.is_connected() is False


def test_connected_when_profile_dir_has_history(edge_profile: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    assert conn.is_connected() is True


def test_connected_with_only_bookmarks(tmp_path: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Bookmarks").write_text("{}", encoding="utf-8")
    conn = EdgeConnector(profile_path=str(profile))
    assert conn.is_connected() is True


def test_sync_yields_history_and_bookmarks(edge_profile: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    docs: List[Document] = list(conn.sync())

    titles = {d.title for d in docs}
    # Two visible history rows + three bookmarks = five docs.
    assert "Hacker News" in titles
    assert "Edge repo" in titles
    assert "Anthropic" in titles
    assert "GitHub" in titles
    assert "Wikipedia" in titles


def test_sync_skips_hidden_history(edge_profile: Path) -> None:
    """Rows with hidden=1 must not be yielded."""
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    docs = list(conn.sync())
    titles = {d.title for d in docs}
    assert "Hidden" not in titles


def test_sync_history_doc_shape(edge_profile: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    docs = list(conn.sync())
    hn = next(d for d in docs if d.title == "Hacker News")
    assert hn.source == "edge"
    assert hn.doc_type == "visit"
    assert hn.url == "https://news.ycombinator.com"
    assert hn.metadata["kind"] == "history"
    assert hn.metadata["visit_count"] == 10


def test_sync_bookmark_doc_shape(edge_profile: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    docs = list(conn.sync())
    github = next(d for d in docs if d.title == "GitHub")
    assert github.source == "edge"
    assert github.doc_type == "bookmark"
    assert github.url == "https://github.com"
    assert github.metadata["kind"] == "bookmark"
    # Folder path includes the root name + nested folder.
    assert "Dev" in github.metadata["folder_path"]


def test_sync_respects_history_limit(tmp_path: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    profile = tmp_path / "profile"
    profile.mkdir()
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": i,
            "url": f"https://example.com/{i}",
            "title": f"page {i}",
            "last_visit_time": _datetime_to_webkit(now - timedelta(minutes=i)),
        }
        for i in range(50)
    ]
    _make_history_db(profile / "History", rows)

    conn = EdgeConnector(profile_path=str(profile), history_limit=5)
    history_docs = [d for d in conn.sync() if d.doc_type == "visit"]
    assert len(history_docs) == 5


def test_sync_respects_since(edge_profile: Path) -> None:
    """Items older than *since* are filtered out for both history and bookmarks."""
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    docs = list(conn.sync(since=cutoff))
    titles = {d.title for d in docs}
    # Newer than 3 days: Hacker News (1h), Edge repo (2 days) — in.
    # Older than 3 days: Anthropic (10 days), GitHub (5 days), Wikipedia
    # (30 days) — out.
    assert "Hacker News" in titles
    assert "Edge repo" in titles
    assert "Anthropic" not in titles
    assert "GitHub" not in titles
    assert "Wikipedia" not in titles


def test_walk_bookmarks_handles_empty_roots() -> None:
    """A bookmark file with no children still yields cleanly."""
    from openjarvis.connectors.edge import _walk_bookmarks  # noqa: PLC0415

    items = list(_walk_bookmarks({"roots": {}}))
    assert items == []


def test_webkit_to_datetime_zero_returns_unix_epoch() -> None:
    """Placeholder zero timestamps map to 1970-01-01, not 1601-01-01."""
    from openjarvis.connectors.edge import _webkit_to_datetime  # noqa: PLC0415

    assert _webkit_to_datetime(0).year == 1970


def test_disconnect_clears_profile_path(edge_profile: Path) -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    conn = EdgeConnector(profile_path=str(edge_profile))
    assert conn.is_connected() is True
    conn.disconnect()
    assert conn.is_connected() is False


def test_registry() -> None:
    from openjarvis.connectors.edge import EdgeConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("edge", EdgeConnector)
    assert ConnectorRegistry.contains("edge")
    cls = ConnectorRegistry.get("edge")
    assert cls.connector_id == "edge"
