"""OneNote connector — syncs notebook pages via the Microsoft Graph API.

Uses the v1.0 Graph endpoints under ``/me/onenote/pages`` to enumerate every
page the signed-in user can read, then fetches each page's HTML body and
strips it down to plain text for ingestion.

Authentication piggybacks on the shared
:mod:`openjarvis.connectors.ms_graph_auth` helper so the same Azure App
Registration powers OneNote, OneDrive, and Microsoft To Do without three
separate consent flows.

All network calls are isolated in module-level functions
(``_onenote_api_*``) so tests can patch them without exercising the network.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
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

_DEFAULT_CREDENTIALS_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "onenote.json")
_ONENOTE_SCOPES: List[str] = [
    "offline_access",
    "Notes.Read",
    "Notes.Read.All",
]

# Graph returns OneNote pages with @odata.nextLink for cursor-style pagination.
_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Module-level API helpers (mockable in tests)
# ---------------------------------------------------------------------------


def _onenote_api_list_pages(
    token: str,
    *,
    next_link: Optional[str] = None,
) -> Dict[str, Any]:
    """Call ``/me/onenote/pages`` (or follow an opaque next_link cursor)."""
    if next_link:
        # next_link is an absolute URL — Graph already includes the auth /
        # query params, but we still need to attach the bearer token.
        url = next_link
        params: Dict[str, Any] = {}
    else:
        url = f"{MS_GRAPH_API_BASE}/me/onenote/pages"
        params = {
            "$top": _PAGE_SIZE,
            "$orderby": "lastModifiedDateTime desc",
            "$select": (
                "id,title,createdDateTime,lastModifiedDateTime,"
                "contentUrl,links,parentSection"
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


def _onenote_api_get_page_content(token: str, page_id: str) -> str:
    """Fetch the HTML body of a single OneNote page."""
    resp = httpx.get(
        f"{MS_GRAPH_API_BASE}/me/onenote/pages/{page_id}/content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# HTML → text conversion
# ---------------------------------------------------------------------------

# OneNote returns clean-ish HTML, but we don't want to require BeautifulSoup
# as a hard dependency. A pair of regexes is enough: drop scripts/styles
# entirely (their content is never useful), then strip every other tag.
_HTML_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WHITESPACE_RE = re.compile(r"[ \t]*\n[ \t]*")
_HTML_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """Convert OneNote page HTML into something the embedding model can chew on.

    Strips ``<script>`` / ``<style>`` blocks (their bodies are noise), unwraps
    every other tag to plain text, collapses runs of blank lines, and HTML-
    unescapes entities. Block-level tags are converted to newlines so list
    items and paragraphs don't all run together on one line.
    """
    if not html:
        return ""
    cleaned = _HTML_SCRIPT_STYLE_RE.sub("", html)
    # Treat each closing block tag as a line break so the text has structure.
    cleaned = re.sub(
        r"</(p|div|li|h[1-6]|tr|br)\s*>",
        "\n",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = unescape(cleaned)
    cleaned = _HTML_WHITESPACE_RE.sub("\n", cleaned)
    cleaned = _HTML_BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def _parse_iso_datetime(dt_str: str) -> datetime:
    """Parse Graph's ISO-8601 timestamps (UTC) into ``datetime`` objects."""
    if not dt_str:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# OneNoteConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("onenote")
class OneNoteConnector(BaseConnector):
    """Connector that syncs OneNote notebook pages via Microsoft Graph.

    The connection model mirrors the Google connectors: users register an
    Azure app, paste ``client_id:client_secret`` into the credentials field,
    and OpenJarvis runs the browser-based OAuth flow in the background.

    Parameters
    ----------
    credentials_path:
        Path to the JSON file where OAuth tokens are stored.  Defaults to
        ``~/.openjarvis/connectors/onenote.json``.
    """

    connector_id = "onenote"
    display_name = "OneNote"
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
        """Return ``True`` only when a real access_token is on disk."""
        tokens = load_tokens(self._credentials_path)
        if tokens is None:
            return False
        return bool(tokens.get("access_token") or tokens.get("token"))

    def disconnect(self) -> None:
        """Delete the stored credentials file."""
        delete_tokens(self._credentials_path)

    def auth_url(self) -> str:
        """Return the Azure Portal URL where users register an app."""
        return (
            "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/"
            "ApplicationsListBlade"
        )

    def handle_callback(self, code: str) -> None:
        """Process the credential paste.

        If *code* looks like ``client_id:client_secret`` we persist those
        credentials and spawn the browser-based OAuth flow on a background
        thread (so the HTTP request that fed us the paste isn't blocked
        for two full minutes). Otherwise we treat the value as a raw access
        token and store it as-is — useful for tests and for users who
        already have a token from another tool.
        """
        code = code.strip()
        if ":" in code and not code.startswith(("ey", "Bearer ")):
            # Looks like client_id:client_secret. Azure client IDs are
            # GUIDs (contain dashes) and secrets contain a mix of chars,
            # but we don't want to be brittle about format — the colon-and-
            # not-a-JWT heuristic is good enough for the UI's "paste your
            # Azure creds" flow.
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
                        scopes=_ONENOTE_SCOPES,
                        credentials_path=self._credentials_path,
                    )
                except Exception:  # noqa: BLE001 — best-effort background work
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
        """Yield :class:`Document` objects for every readable OneNote page."""
        tokens = load_tokens(self._credentials_path)
        if not tokens or not (tokens.get("access_token") or tokens.get("token")):
            return

        next_link: Optional[str] = cursor
        synced = 0

        while True:
            page_resp = call_with_refresh(
                _onenote_api_list_pages,
                self._credentials_path,
                next_link=next_link,
            )
            pages: List[Dict[str, Any]] = page_resp.get("value", [])

            for page in pages:
                page_id: str = page.get("id", "")
                if not page_id:
                    continue

                title: str = page.get("title", "") or "(Untitled OneNote page)"
                modified_str: str = page.get("lastModifiedDateTime", "")
                timestamp = _parse_iso_datetime(modified_str)

                if since is not None:
                    since_aware = since
                    if since.tzinfo is None and timestamp.tzinfo is not None:
                        since_aware = since.replace(tzinfo=timezone.utc)
                    if timestamp < since_aware:
                        continue

                # Graph's "web URL" lives under links.oneNoteWebUrl.href.
                links = page.get("links", {}) or {}
                web_link = (links.get("oneNoteWebUrl") or {}).get("href", "")

                parent = page.get("parentSection", {}) or {}
                section_name = parent.get("displayName", "")

                try:
                    html_body = call_with_refresh(
                        _onenote_api_get_page_content,
                        self._credentials_path,
                        page_id,
                    )
                except httpx.HTTPError:
                    # A single bad page shouldn't fail the entire sync —
                    # log a placeholder so the user sees something in
                    # results and skip.
                    html_body = f"[Failed to fetch OneNote page: {title}]"

                content = _html_to_text(html_body)
                if not content:
                    content = f"[OneNote page: {title}]"

                doc = Document(
                    doc_id=f"onenote:{page_id}",
                    source="onenote",
                    doc_type="note",
                    content=content,
                    title=title,
                    timestamp=timestamp,
                    url=web_link or None,
                    metadata={
                        "page_id": page_id,
                        "section": section_name,
                    },
                )
                synced += 1
                yield doc

            next_link = page_resp.get("@odata.nextLink")
            if not next_link:
                self._last_cursor = None
                break
            self._last_cursor = next_link

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
                name="onenote_search_pages",
                description=(
                    "Search OneNote pages the user can read by keyword. "
                    "Returns matching page titles and OneNote web links."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query string",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of pages to return",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
                category="knowledge",
            ),
            ToolSpec(
                name="onenote_get_page",
                description="Retrieve the plain-text content of a OneNote page by ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "page_id": {
                            "type": "string",
                            "description": "OneNote page ID returned by onenote_search_pages",
                        },
                    },
                    "required": ["page_id"],
                },
                category="knowledge",
            ),
        ]
