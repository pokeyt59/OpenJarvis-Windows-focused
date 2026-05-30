"""Microsoft Edge browser connector — local history and bookmarks.

Reads two files from the user's Edge profile directory:

* ``History`` — a SQLite database whose ``urls`` table holds every visited
  URL with its title, visit count, and last-visit timestamp.
* ``Bookmarks`` — a JSON file with the bookmark-bar / other / synced trees,
  each node carrying a name, URL, and added timestamp.

Edge keeps the History file open and write-locked while the browser is
running, so we copy it to a temp file before opening it with sqlite3 — this
is the standard pattern Chromium-based browsers (Edge, Chrome, Brave, Arc)
require. Bookmarks is a flat JSON snapshot Edge re-writes atomically, so
we read it in place.

All timestamps Chromium writes are "Webkit time" — microseconds since
1601-01-01 UTC. We convert to ``datetime`` at parse time so callers see
normal Python timestamps.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.registry import ConnectorRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Webkit / Chromium epoch.
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# Cap the number of history rows we ingest in a single sync. Edge profiles
# accumulate tens of thousands of rows; for a personal knowledge base the
# most-recent few thousand cover the actually-useful "what was I reading
# last week" surface area.
_DEFAULT_HISTORY_LIMIT = 5000


def _default_edge_profile_dir() -> Optional[Path]:
    """Best-effort lookup of the default Edge profile dir on this machine.

    Returns ``None`` when no plausible path exists (e.g. running on Linux,
    or Edge has never been installed on this Windows box).
    """
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidate = Path(localappdata) / "Microsoft" / "Edge" / "User Data" / "Default"
        if candidate.is_dir():
            return candidate

    # macOS fallback (some users dual-boot or run Edge on Mac).
    home = Path.home()
    mac_candidate = (
        home / "Library" / "Application Support" / "Microsoft Edge" / "Default"
    )
    if mac_candidate.is_dir():
        return mac_candidate

    # Linux fallback.
    linux_candidate = home / ".config" / "microsoft-edge" / "Default"
    if linux_candidate.is_dir():
        return linux_candidate

    return None


def _webkit_to_datetime(microseconds: int) -> datetime:
    """Convert a Webkit timestamp (μs since 1601-01-01 UTC) to a datetime.

    Returns the Unix epoch for ``0`` / negative values rather than throwing —
    history rows occasionally carry placeholder zeros.
    """
    if not microseconds or microseconds < 0:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return _WEBKIT_EPOCH + timedelta(microseconds=microseconds)
    except OverflowError:
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Bookmark traversal
# ---------------------------------------------------------------------------


def _walk_bookmark_node(
    node: Dict[str, Any],
    *,
    folder_path: str = "",
) -> Iterator[Dict[str, Any]]:
    """Walk a bookmark JSON node recursively, yielding every URL bookmark.

    Each yielded dict carries ``url``, ``name``, ``date_added`` (Webkit), and
    ``folder_path`` (slash-joined names of containing folders).
    """
    node_type = node.get("type", "")
    name = node.get("name", "")

    if node_type == "url":
        yield {
            "url": node.get("url", ""),
            "name": name,
            "date_added": node.get("date_added", "0"),
            "folder_path": folder_path,
        }
    elif node_type == "folder":
        sub_path = f"{folder_path}/{name}" if folder_path else name
        for child in node.get("children", []):
            yield from _walk_bookmark_node(child, folder_path=sub_path)


def _walk_bookmarks(bookmarks_json: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield every URL bookmark across all roots (bookmark_bar, other, synced)."""
    roots = bookmarks_json.get("roots", {})
    for root_name, root_node in roots.items():
        if not isinstance(root_node, dict):
            continue
        # The top-level root nodes themselves are folders; walk their children.
        for child in root_node.get("children", []):
            yield from _walk_bookmark_node(child, folder_path=root_name)


# ---------------------------------------------------------------------------
# EdgeConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("edge")
class EdgeConnector(BaseConnector):
    """Connector that reads Microsoft Edge browsing history and bookmarks.

    Parameters
    ----------
    profile_path:
        Path to the Edge profile directory (the one containing ``History``
        and ``Bookmarks``). If empty, auto-detects from ``%LOCALAPPDATA%``
        on Windows or the OS-equivalent location elsewhere.
    history_limit:
        Maximum number of history rows to ingest per sync. Defaults to
        the ``_DEFAULT_HISTORY_LIMIT`` module constant (5000).
    """

    connector_id = "edge"
    display_name = "Microsoft Edge"
    auth_type = "filesystem"

    def __init__(
        self,
        profile_path: str = "",
        *,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> None:
        if profile_path:
            self._profile_path: Optional[Path] = Path(profile_path)
        else:
            self._profile_path = _default_edge_profile_dir()
        self._history_limit = history_limit
        self._items_synced: int = 0
        self._items_total: int = 0
        self._last_sync: Optional[datetime] = None

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True if the profile dir exists and contains at least one of
        the expected files.

        We accept either ``History`` or ``Bookmarks`` on its own — fresh
        Edge installs may have only one until the user starts browsing.
        """
        if not self._profile_path or not self._profile_path.is_dir():
            return False
        history = self._profile_path / "History"
        bookmarks = self._profile_path / "Bookmarks"
        return history.is_file() or bookmarks.is_file()

    def disconnect(self) -> None:
        self._profile_path = None

    def sync(
        self,
        *,
        since: Optional[datetime] = None,
        cursor: Optional[str] = None,  # noqa: ARG002 — local read, no cursor
    ) -> Iterator[Document]:
        """Yield :class:`Document` per visited URL and per bookmark."""
        if not self.is_connected():
            return
        assert self._profile_path is not None  # narrowed by is_connected

        synced = 0

        # ---- History ----
        history_path = self._profile_path / "History"
        if history_path.is_file():
            for doc in self._iter_history(history_path, since=since):
                synced += 1
                yield doc

        # ---- Bookmarks ----
        bookmarks_path = self._profile_path / "Bookmarks"
        if bookmarks_path.is_file():
            for doc in self._iter_bookmarks(bookmarks_path, since=since):
                synced += 1
                yield doc

        self._items_synced = synced
        self._last_sync = datetime.now(tz=timezone.utc)

    def sync_status(self) -> SyncStatus:
        return SyncStatus(
            state="idle",
            items_synced=self._items_synced,
            items_total=self._items_total,
            last_sync=self._last_sync,
        )

    # ------------------------------------------------------------------
    # Internal: history
    # ------------------------------------------------------------------

    def _iter_history(
        self,
        history_path: Path,
        *,
        since: Optional[datetime],
    ) -> Iterator[Document]:
        """Open the History DB (via temp copy) and yield one Document per URL row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "History"
            try:
                # Edge keeps an exclusive lock while running; copying lets us
                # open the snapshot without contending with the live process.
                shutil.copy2(history_path, tmp_db)
            except (OSError, PermissionError):
                return

            try:
                conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            except sqlite3.OperationalError:
                return

            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, url, title, visit_count, typed_count, last_visit_time
                    FROM urls
                    WHERE hidden = 0
                    ORDER BY last_visit_time DESC
                    LIMIT ?
                    """,
                    (self._history_limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                conn.close()
                return

            conn.close()

            for row in rows:
                last_visit_time = row["last_visit_time"]
                visited_at = _webkit_to_datetime(last_visit_time)
                if since is not None:
                    since_aware = since
                    if since.tzinfo is None:
                        since_aware = since.replace(tzinfo=timezone.utc)
                    if visited_at < since_aware:
                        continue

                title = row["title"] or row["url"]
                url = row["url"] or ""
                visit_count = row["visit_count"] or 0
                typed_count = row["typed_count"] or 0

                # The "content" is intentionally just title + URL — we have
                # no body text without scraping the page, which we won't do.
                content = f"{title}\n{url}"

                yield Document(
                    doc_id=f"edge:history:{row['id']}",
                    source="edge",
                    doc_type="visit",
                    content=content,
                    title=title,
                    timestamp=visited_at,
                    url=url,
                    metadata={
                        "url_id": row["id"],
                        "visit_count": visit_count,
                        "typed_count": typed_count,
                        "kind": "history",
                    },
                )

    # ------------------------------------------------------------------
    # Internal: bookmarks
    # ------------------------------------------------------------------

    def _iter_bookmarks(
        self,
        bookmarks_path: Path,
        *,
        since: Optional[datetime],
    ) -> Iterator[Document]:
        """Parse the Bookmarks JSON file and yield one Document per bookmark."""
        try:
            raw = bookmarks_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return

        for bm in _walk_bookmarks(data):
            url = bm["url"]
            if not url:
                continue
            name = bm["name"] or url
            try:
                added_micros = int(bm["date_added"])
            except (TypeError, ValueError):
                added_micros = 0
            added_at = _webkit_to_datetime(added_micros)

            if since is not None:
                since_aware = since
                if since.tzinfo is None:
                    since_aware = since.replace(tzinfo=timezone.utc)
                if added_at < since_aware:
                    continue

            folder_path = bm["folder_path"]
            content = f"{name}\n{url}\n[folder: {folder_path}]"

            # Stable doc_id: hash isn't necessary — folder_path|url is unique
            # because Edge enforces unique URLs within a folder.
            doc_id_key = f"{folder_path}|{url}"

            yield Document(
                doc_id=f"edge:bookmark:{doc_id_key}",
                source="edge",
                doc_type="bookmark",
                content=content,
                title=name,
                timestamp=added_at,
                url=url,
                metadata={
                    "folder_path": folder_path,
                    "kind": "bookmark",
                },
            )
