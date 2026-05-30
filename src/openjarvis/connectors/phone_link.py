"""Phone Link connector — read Android SMS that Windows has cached locally.

Microsoft's "Phone Link" app (formerly "Your Phone") bridges an Android
device to Windows and caches recent SMS/MMS conversations in a local
SQLite database under
``%LOCALAPPDATA%\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe\\LocalState``.

THIS CONNECTOR IS BEST-EFFORT — be warned:

* Microsoft has never documented the local schema.
* The schema has changed across Phone Link versions; column and table names
  have shifted multiple times since the app's "Your Phone" days.
* Some builds store messages only in memory and never persist them locally.
* On some accounts, RCS / iMessage data is intentionally absent from the
  local cache for privacy reasons.

To stay useful across versions we don't hardcode any single schema. Instead
we:

1. Walk every ``*.db`` file in the LocalState directory.
2. For each DB, look at ``sqlite_master`` for tables whose name looks
   message-like (``%message%``, ``%sms%``, ``%conversation%``).
3. Inspect each candidate table's columns, pick the first that looks like
   a body column (``body`` / ``text`` / ``content`` / ``message``), and
   optionally pick a timestamp column.
4. Yield :class:`Document` rows for every matching row we can extract.

If none of the heuristics match (because Microsoft changed the schema yet
again) the sync simply yields nothing — better silent than crashing.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.registry import ConnectorRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The well-known Phone Link package family name. Hasn't changed since the
# rename from "Your Phone" — Microsoft kept the underlying package id.
_PHONE_LINK_PACKAGE = "Microsoft.YourPhone_8wekyb3d8bbwe"

# Heuristic table name fragments. lower()d for case-insensitive matching.
_MESSAGE_TABLE_FRAGMENTS = ("message", "sms", "conversation", "chat")

# Column names we'll try in order for the body / text content.
_BODY_COLUMN_CANDIDATES = (
    "body",
    "text",
    "content",
    "message",
    "message_text",
    "body_text",
)
_TIMESTAMP_COLUMN_CANDIDATES = (
    "timestamp",
    "received_timestamp",
    "received_at",
    "date",
    "time",
    "created_at",
    "sent_timestamp",
    "modified_time",
)
_SENDER_COLUMN_CANDIDATES = (
    "sender",
    "from_address",
    "address",
    "from",
    "sender_address",
    "participant",
)
_THREAD_COLUMN_CANDIDATES = (
    "thread_id",
    "conversation_id",
    "chat_id",
    "thread",
)
_ID_COLUMN_CANDIDATES = (
    "id",
    "message_id",
    "rowid",
    "pk",
)


def _default_phone_link_dir() -> Optional[Path]:
    """Return the default Phone Link LocalState dir or None when not present."""
    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        return None
    candidate = (
        Path(localappdata) / "Packages" / _PHONE_LINK_PACKAGE / "LocalState"
    )
    return candidate if candidate.is_dir() else None


def _normalise_timestamp(value: object) -> datetime:
    """Best-effort conversion of an unknown timestamp value to a datetime.

    Phone Link timestamps have appeared as: epoch seconds, epoch
    milliseconds, ISO strings, and Webkit microseconds across versions.
    We accept whatever looks plausible.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        v = float(value)
        # Heuristic: > 1e15 looks like Webkit microseconds since 1601.
        if v > 1e15:
            try:
                base = datetime(1601, 1, 1, tzinfo=timezone.utc)
                from datetime import timedelta

                return base + timedelta(microseconds=v)
            except OverflowError:
                return datetime.now(tz=timezone.utc)
        # > 1e12 looks like epoch milliseconds.
        if v > 1e12:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
        # Otherwise treat as epoch seconds.
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.now(tz=timezone.utc)
    if isinstance(value, str):
        if not value:
            return datetime.now(tz=timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return datetime.now(tz=timezone.utc)
    return datetime.now(tz=timezone.utc)


def _pick_column(
    columns: List[str],
    candidates: Tuple[str, ...],
) -> Optional[str]:
    """Return the first *candidates* entry present in *columns* (case-insensitive)."""
    lower_to_real = {c.lower(): c for c in columns}
    for candidate in candidates:
        real = lower_to_real.get(candidate.lower())
        if real:
            return real
    return None


# ---------------------------------------------------------------------------
# PhoneLinkConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("phone_link")
class PhoneLinkConnector(BaseConnector):
    """Best-effort indexer for Phone Link's local SMS cache.

    Parameters
    ----------
    data_dir:
        Optional override for the LocalState dir. Empty → auto-detect from
        ``%LOCALAPPDATA%``. Tests pass a tmp_path.
    """

    connector_id = "phone_link"
    display_name = "Phone Link (Android SMS)"
    auth_type = "filesystem"

    def __init__(self, data_dir: str = "") -> None:
        if data_dir:
            self._data_dir: Optional[Path] = Path(data_dir)
        else:
            self._data_dir = _default_phone_link_dir()
        self._items_synced: int = 0
        self._items_total: int = 0
        self._last_sync: Optional[datetime] = None

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True if the data dir exists and contains at least one .db file."""
        if not self._data_dir or not self._data_dir.is_dir():
            return False
        try:
            return any(p.suffix.lower() == ".db" for p in self._data_dir.iterdir())
        except OSError:
            return False

    def disconnect(self) -> None:
        self._data_dir = None

    def sync(
        self,
        *,
        since: Optional[datetime] = None,
        cursor: Optional[str] = None,  # noqa: ARG002 — local read, no cursor
    ) -> Iterator[Document]:
        """Walk every .db file looking for message-like rows to yield."""
        if not self.is_connected():
            return
        assert self._data_dir is not None  # narrowed by is_connected

        synced = 0

        for db_file in sorted(self._data_dir.glob("*.db")):
            try:
                yield_count = 0
                for doc in self._iter_db(db_file, since=since):
                    synced += 1
                    yield_count += 1
                    yield doc
                logger.debug(
                    "Phone Link: %d rows from %s", yield_count, db_file.name
                )
            except Exception as exc:  # noqa: BLE001 — schema/permissions vary
                logger.warning(
                    "Phone Link: skipped %s due to error: %s", db_file.name, exc
                )

        self._items_synced = synced
        self._last_sync = datetime.now(tz=timezone.utc)

    def sync_status(self) -> SyncStatus:
        return SyncStatus(
            state="idle",
            items_synced=self._items_synced,
            last_sync=self._last_sync,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _iter_db(
        self,
        db_path: Path,
        *,
        since: Optional[datetime],
    ) -> Iterator[Document]:
        """Open *db_path* read-only via a temp copy and yield Documents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / db_path.name
            try:
                shutil.copy2(db_path, tmp_db)
            except OSError:
                return

            try:
                conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            except sqlite3.OperationalError:
                return

            try:
                conn.row_factory = sqlite3.Row
                # Find candidate message tables.
                table_rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                candidate_tables: List[str] = []
                for row in table_rows:
                    name = row[0]
                    if any(frag in name.lower() for frag in _MESSAGE_TABLE_FRAGMENTS):
                        candidate_tables.append(name)

                for table in candidate_tables:
                    yield from self._iter_table(
                        conn, db_path.name, table, since=since
                    )
            finally:
                conn.close()

    def _iter_table(
        self,
        conn: sqlite3.Connection,
        db_name: str,
        table: str,
        *,
        since: Optional[datetime],
    ) -> Iterator[Document]:
        """Yield Documents from *table* using best-guess column mapping."""
        try:
            info_rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            return
        columns: List[str] = [r[1] for r in info_rows]
        if not columns:
            return

        body_col = _pick_column(columns, _BODY_COLUMN_CANDIDATES)
        if not body_col:
            # Without text content there's nothing to index for the
            # knowledge store.
            return

        ts_col = _pick_column(columns, _TIMESTAMP_COLUMN_CANDIDATES)
        sender_col = _pick_column(columns, _SENDER_COLUMN_CANDIDATES)
        thread_col = _pick_column(columns, _THREAD_COLUMN_CANDIDATES)
        id_col = _pick_column(columns, _ID_COLUMN_CANDIDATES) or "rowid"

        select_cols: List[str] = [f'"{id_col}"', f'"{body_col}"']
        if ts_col:
            select_cols.append(f'"{ts_col}"')
        if sender_col:
            select_cols.append(f'"{sender_col}"')
        if thread_col:
            select_cols.append(f'"{thread_col}"')

        order_clause = f' ORDER BY "{ts_col}" DESC' if ts_col else ""
        sql = f'SELECT {", ".join(select_cols)} FROM "{table}"{order_clause}'

        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return

        for row in rows:
            body = row[body_col] if body_col in row.keys() else None
            if not body:
                continue
            row_id = row[id_col] if id_col in row.keys() else None
            if row_id is None:
                continue

            if ts_col and ts_col in row.keys():
                timestamp = _normalise_timestamp(row[ts_col])
            else:
                timestamp = datetime.now(tz=timezone.utc)

            if since is not None:
                since_aware = since
                if since.tzinfo is None:
                    since_aware = since.replace(tzinfo=timezone.utc)
                if timestamp < since_aware:
                    continue

            sender = ""
            if sender_col and sender_col in row.keys():
                sender = str(row[sender_col] or "")
            thread = ""
            if thread_col and thread_col in row.keys():
                thread = str(row[thread_col] or "")

            yield Document(
                doc_id=f"phone_link:{db_name}:{table}:{row_id}",
                source="phone_link",
                doc_type="message",
                content=str(body),
                title=f"SMS from {sender}" if sender else "SMS",
                author=sender,
                timestamp=timestamp,
                thread_id=thread or None,
                metadata={
                    "db_name": db_name,
                    "table": table,
                    "row_id": row_id,
                },
            )
