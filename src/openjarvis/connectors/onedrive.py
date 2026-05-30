"""OneDrive connector — syncs files via the Microsoft Graph delta API.

Walks the user's OneDrive recursively via ``/me/drive/root/delta`` (incremental
sync friendly — the response carries a ``@odata.deltaLink`` we persist as the
sync cursor for the next run).

For text-extractable files (Markdown, plain text, CSV, JSON, HTML) we download
the body and store it directly. For Office documents and binaries we yield a
metadata-only document carrying the file name and web URL so users can still
search by name and click through to the file in OneDrive.

Authentication is shared with OneNote / Microsoft To Do via
:mod:`openjarvis.connectors.ms_graph_auth` — one Azure app, one consent
flow, three connectors.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import httpx

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.connectors.ms_graph_auth import (
    MS_GRAPH_API_BASE,
    call_with_refresh,
    run_ms_graph_oauth_flow,
)
from openjarvis.connectors.oauth import delete_tokens, load_tokens, save_tokens
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.tools._stubs import ToolSpec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CREDENTIALS_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "onedrive.json")
_ONEDRIVE_SCOPES: List[str] = [
    "offline_access",
    "Files.Read",
    "Files.Read.All",
]

# Files larger than this aren't downloaded — they're stored as metadata only.
# 5 MiB is large enough for typical text/markdown/CSV/source code documents
# but small enough that we don't burn RAM on PowerPoint decks or videos.
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024

# MIME types we treat as plain text. Extensions checked separately for
# files Graph returns with a generic application/octet-stream type.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/x-toml",
    "application/x-sh",
    "application/javascript",
}
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".html",
    ".htm",
    ".log",
    ".ini",
    ".cfg",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
}


# ---------------------------------------------------------------------------
# Module-level API helpers (mockable in tests)
# ---------------------------------------------------------------------------


def _onedrive_api_delta(
    token: str,
    *,
    next_link: Optional[str] = None,
) -> Dict[str, Any]:
    """Call ``/me/drive/root/delta`` or follow a delta/next link cursor.

    Graph returns:
    * ``value``      — list of changed items (files & folders)
    * ``@odata.nextLink``  — present mid-pagination, follow it for more results
    * ``@odata.deltaLink`` — present at the end; persist for incremental syncs
    """
    if next_link:
        url = next_link
        params: Dict[str, Any] = {}
    else:
        url = f"{MS_GRAPH_API_BASE}/me/drive/root/delta"
        params = {
            "$select": (
                "id,name,size,file,folder,webUrl,"
                "createdDateTime,lastModifiedDateTime,parentReference"
            ),
        }
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _onedrive_api_download(token: str, item_id: str) -> bytes:
    """Download the raw bytes of a OneDrive file by item ID."""
    resp = httpx.get(
        f"{MS_GRAPH_API_BASE}/me/drive/items/{item_id}/content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_text_file(name: str, mime_type: str) -> bool:
    """Return True if *name* / *mime_type* looks like an extractable text file."""
    if mime_type:
        if any(mime_type.startswith(p) for p in _TEXT_MIME_PREFIXES):
            return True
        if mime_type in _TEXT_MIME_EXACT:
            return True
    lower_name = name.lower()
    for ext in _TEXT_EXTENSIONS:
        if lower_name.endswith(ext):
            return True
    return False


def _parse_iso_datetime(dt_str: str) -> datetime:
    """Parse Graph's ISO-8601 timestamps (UTC) into datetimes."""
    if not dt_str:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(tz=timezone.utc)


def _decode_bytes(data: bytes) -> str:
    """Best-effort decode of downloaded bytes to text.

    Tries UTF-8 first (covers ~all modern text files), falls back to
    cp1252 (the legacy Windows encoding), then UTF-8 with replacement.
    """
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# OneDriveConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("onedrive")
class OneDriveConnector(BaseConnector):
    """Connector that syncs OneDrive files via the Microsoft Graph delta API."""

    connector_id = "onedrive"
    display_name = "OneDrive"
    auth_type = "oauth"

    def __init__(self, credentials_path: str = "") -> None:
        self._credentials_path: str = credentials_path or _DEFAULT_CREDENTIALS_PATH
        self._items_synced: int = 0
        self._items_total: int = 0
        self._last_sync: Optional[datetime] = None
        self._last_cursor: Optional[str] = None

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        tokens = load_tokens(self._credentials_path)
        if tokens is None:
            return False
        return bool(tokens.get("access_token") or tokens.get("token"))

    def disconnect(self) -> None:
        delete_tokens(self._credentials_path)

    def auth_url(self) -> str:
        return (
            "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/"
            "ApplicationsListBlade"
        )

    def handle_callback(self, code: str) -> None:
        """Persist credentials and (if a client_id:client_secret pair was
        pasted) kick off the browser OAuth flow on a background thread.

        Same shape as ``OneNoteConnector.handle_callback`` — kept duplicated
        rather than extracted into a shared helper because the credential
        files and scope sets differ per connector and we want each to be
        legible in isolation.
        """
        code = code.strip()
        if ":" in code and not code.startswith(("ey", "Bearer ")):
            client_id, _, client_secret = code.partition(":")
            client_id = client_id.strip()
            client_secret = client_secret.strip()
            save_tokens(
                self._credentials_path,
                {"client_id": client_id, "client_secret": client_secret},
            )
            import threading

            def _run() -> None:
                try:
                    run_ms_graph_oauth_flow(
                        client_id=client_id,
                        client_secret=client_secret,
                        scopes=_ONEDRIVE_SCOPES,
                        credentials_path=self._credentials_path,
                    )
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_run, daemon=True).start()
        else:
            save_tokens(self._credentials_path, {"token": code})

    def sync(
        self,
        *,
        since: Optional[datetime] = None,
        cursor: Optional[str] = None,
    ) -> Iterator[Document]:
        """Walk OneDrive via delta and yield :class:`Document` per file."""
        tokens = load_tokens(self._credentials_path)
        if not tokens or not (tokens.get("access_token") or tokens.get("token")):
            return

        next_link: Optional[str] = cursor
        synced = 0

        while True:
            resp = call_with_refresh(
                _onedrive_api_delta,
                self._credentials_path,
                next_link=next_link,
            )
            items: List[Dict[str, Any]] = resp.get("value", [])

            for item in items:
                # Folders are returned as items too — skip them outright.
                if "folder" in item:
                    continue
                # The "root" item itself shows up in delta with no file/folder
                # marker; skip anything that isn't a file.
                if "file" not in item:
                    continue
                # Tombstones (deletes) carry a "deleted" facet. Skip — we
                # don't currently support deleting from the knowledge store
                # via sync; the UI's "disconnect" button is the way to wipe.
                if item.get("deleted"):
                    continue

                item_id: str = item.get("id", "")
                if not item_id:
                    continue

                name: str = item.get("name", "") or "(Unnamed file)"
                mime_type: str = (item.get("file") or {}).get("mimeType", "")
                size: int = int(item.get("size", 0) or 0)
                modified_str: str = item.get("lastModifiedDateTime", "")
                timestamp = _parse_iso_datetime(modified_str)
                web_url: str = item.get("webUrl", "") or ""

                if since is not None:
                    since_aware = since
                    if since.tzinfo is None and timestamp.tzinfo is not None:
                        since_aware = since.replace(tzinfo=timezone.utc)
                    if timestamp < since_aware:
                        continue

                parent = item.get("parentReference", {}) or {}
                folder_path: str = parent.get("path", "") or ""

                # Decide whether to download the body or store metadata only.
                if _is_text_file(name, mime_type) and 0 < size <= _MAX_DOWNLOAD_BYTES:
                    try:
                        raw = call_with_refresh(
                            _onedrive_api_download,
                            self._credentials_path,
                            item_id,
                        )
                        content = _decode_bytes(raw)
                    except httpx.HTTPError:
                        content = f"[File: {name}] ({mime_type or 'unknown type'})"
                else:
                    content = f"[File: {name}] ({mime_type or 'binary'})"

                doc = Document(
                    doc_id=f"onedrive:{item_id}",
                    source="onedrive",
                    doc_type="file",
                    content=content,
                    title=name,
                    timestamp=timestamp,
                    url=web_url or None,
                    metadata={
                        "item_id": item_id,
                        "mime_type": mime_type,
                        "size_bytes": size,
                        "folder_path": folder_path,
                    },
                )
                synced += 1
                yield doc

            next_page = resp.get("@odata.nextLink")
            delta_link = resp.get("@odata.deltaLink")
            if next_page:
                next_link = next_page
                continue
            # Final page: persist the deltaLink so the next sync is incremental.
            self._last_cursor = delta_link
            break

        self._items_synced = synced
        self._last_sync = datetime.now(tz=timezone.utc)

    def sync_status(self) -> SyncStatus:
        return SyncStatus(
            state="idle",
            items_synced=self._items_synced,
            last_sync=self._last_sync,
            cursor=self._last_cursor,
        )

    # ------------------------------------------------------------------
    # MCP tools
    # ------------------------------------------------------------------

    def mcp_tools(self) -> List[ToolSpec]:
        return [
            ToolSpec(
                name="onedrive_search_files",
                description=(
                    "Search OneDrive files by name keyword. "
                    "Returns matching filenames and OneDrive web URLs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Filename keyword to search for",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of files to return",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
                category="productivity",
            ),
        ]
