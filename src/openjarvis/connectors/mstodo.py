"""Microsoft To Do connector — syncs task lists via Microsoft Graph.

Walks every readable to-do list, then walks every task in each list, yielding
one :class:`Document` per task. The body content (notes) and metadata
(status, due date, importance, list name) are preserved so the digest agent
can answer "what's due today" / "what's overdue" queries.

Authentication is shared with OneNote / OneDrive via
:mod:`openjarvis.connectors.ms_graph_auth` — one Azure app covers all three.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

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

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CREDENTIALS_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "mstodo.json")
_MSTODO_SCOPES: List[str] = [
    "offline_access",
    "Tasks.Read",
]


# ---------------------------------------------------------------------------
# Module-level API helpers (mockable in tests)
# ---------------------------------------------------------------------------


def _mstodo_api_list_lists(token: str) -> Dict[str, Any]:
    """Fetch every readable to-do list (paginated by Graph if needed)."""
    resp = httpx.get(
        f"{MS_GRAPH_API_BASE}/me/todo/lists",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _mstodo_api_list_tasks(
    token: str,
    list_id: str,
    *,
    next_link: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch tasks for *list_id*, or follow an opaque ``@odata.nextLink``."""
    if next_link:
        url = next_link
        params: Dict[str, Any] = {}
    else:
        url = f"{MS_GRAPH_API_BASE}/me/todo/lists/{list_id}/tasks"
        params = {
            # Tasks are most useful sorted by due date so the digest can
            # surface "what's most urgent" without sorting client-side.
            "$top": 100,
        }
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
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


def _format_task_body(task: Dict[str, Any], list_name: str) -> str:
    """Render a task into a human-readable text block for the knowledge store.

    The format puts the title first (the most likely match for a free-text
    search), then the body / notes, then a metadata footer. This keeps the
    first line useful for BM25 ranking while still preserving the surrounding
    detail.
    """
    title = task.get("title", "") or "(Untitled task)"
    body = (task.get("body") or {}).get("content", "")
    status = task.get("status", "")
    importance = task.get("importance", "")
    due_meta = task.get("dueDateTime") or {}
    due_str = due_meta.get("dateTime", "")

    parts: List[str] = [title]
    if body:
        parts.append("")
        parts.append(body.strip())

    footer_bits: List[str] = [f"List: {list_name}" if list_name else ""]
    if status:
        footer_bits.append(f"Status: {status}")
    if importance and importance != "normal":
        footer_bits.append(f"Importance: {importance}")
    if due_str:
        footer_bits.append(f"Due: {due_str}")
    footer = " · ".join(b for b in footer_bits if b)
    if footer:
        parts.append("")
        parts.append(footer)

    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# MSToDoConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("mstodo")
class MSToDoConnector(BaseConnector):
    """Connector that syncs Microsoft To Do tasks via Graph."""

    connector_id = "mstodo"
    display_name = "Microsoft To Do"
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
                        scopes=_MSTODO_SCOPES,
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
        cursor: Optional[str] = None,  # noqa: ARG002 — Graph To Do has no delta
    ) -> Iterator[Document]:
        """Yield one :class:`Document` per to-do task across every list."""
        tokens = load_tokens(self._credentials_path)
        if not tokens or not (tokens.get("access_token") or tokens.get("token")):
            return

        lists_resp = call_with_refresh(_mstodo_api_list_lists, self._credentials_path)
        lists: List[Dict[str, Any]] = lists_resp.get("value", [])

        synced = 0

        for tl in lists:
            list_id: str = tl.get("id", "")
            if not list_id:
                continue
            list_name: str = tl.get("displayName", "") or "(Unnamed list)"

            next_link: Optional[str] = None
            while True:
                task_resp = call_with_refresh(
                    _mstodo_api_list_tasks,
                    self._credentials_path,
                    list_id,
                    next_link=next_link,
                )
                tasks: List[Dict[str, Any]] = task_resp.get("value", [])

                for task in tasks:
                    task_id: str = task.get("id", "")
                    if not task_id:
                        continue
                    title: str = task.get("title", "") or "(Untitled task)"
                    modified_str: str = task.get("lastModifiedDateTime", "")
                    timestamp = _parse_iso_datetime(modified_str)

                    if since is not None:
                        since_aware = since
                        if since.tzinfo is None and timestamp.tzinfo is not None:
                            since_aware = since.replace(tzinfo=timezone.utc)
                        if timestamp < since_aware:
                            continue

                    content = _format_task_body(task, list_name)

                    doc = Document(
                        doc_id=f"mstodo:{task_id}",
                        source="mstodo",
                        doc_type="task",
                        content=content,
                        title=title,
                        timestamp=timestamp,
                        channel=list_name,
                        metadata={
                            "task_id": task_id,
                            "list_id": list_id,
                            "list_name": list_name,
                            "status": task.get("status", ""),
                            "importance": task.get("importance", ""),
                            "due": (task.get("dueDateTime") or {}).get("dateTime", ""),
                        },
                    )
                    synced += 1
                    yield doc

                next_link = task_resp.get("@odata.nextLink")
                if not next_link:
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
                name="mstodo_search_tasks",
                description=(
                    "Search Microsoft To Do tasks by keyword. Returns matching "
                    "task titles, their list, status, and due date."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query against task titles and notes",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of tasks to return",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
                category="productivity",
            ),
        ]
