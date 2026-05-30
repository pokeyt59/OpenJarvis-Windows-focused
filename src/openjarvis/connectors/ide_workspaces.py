"""IDE workspaces connector — recent projects from VS Code and JetBrains IDEs.

Reads where each editor records its "recently opened" projects so the agent
can answer "what was I working on last week" without any browsing-history
tricks.

Two sources are unified into one connector:

* **VS Code family** — walks ``%APPDATA%\\Code\\User\\workspaceStorage`` (and
  the analogous dirs for ``Code - Insiders`` and ``VSCodium``). Each
  subdirectory contains a ``workspace.json`` whose ``folder`` URI points
  at the opened project. Subdirectory mtime gives us the last-touched
  timestamp.

* **JetBrains family** — walks ``%APPDATA%\\JetBrains\\`` for every product
  config dir (``IntelliJIdea2024.1``, ``PyCharm2024.1``, ``WebStorm2024.1``,
  ``RustRover2024.1``, ``GoLand2024.1``, etc.) and parses each one's
  ``options/recentProjects.xml`` for project paths plus their last-opened
  epoch-millisecond timestamps.

Each yielded :class:`Document` represents one recent project: title is the
project's folder name, ``content`` is a readable summary, and ``metadata``
preserves the IDE flavour and absolute path so downstream agents can act
on it (e.g. "cd into the project and run tests").

Tests pass synthetic ``vscode_user_dirs`` / ``jetbrains_dir`` overrides
into the constructor so they don't need a real editor install.
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import unquote

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.registry import ConnectorRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default-path discovery
# ---------------------------------------------------------------------------


def _default_vscode_user_dirs() -> List[Path]:
    """Return every VS Code-family ``User`` dir that exists on this machine.

    Covers stable VS Code, the Insiders channel, and VSCodium. macOS and
    Linux paths are included as fallbacks so the connector isn't strictly
    Windows-only — the *useful* userbase is on Windows but indexing the
    same files on macOS / Linux costs nothing.
    """
    found: List[Path] = []

    # Windows: %APPDATA%\Code\User etc.
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
        for flavour in ("Code", "Code - Insiders", "VSCodium"):
            candidate = base / flavour / "User"
            if candidate.is_dir():
                found.append(candidate)

    # macOS: ~/Library/Application Support/Code/User
    home = Path.home()
    mac_root = home / "Library" / "Application Support"
    if mac_root.is_dir():
        for flavour in ("Code", "Code - Insiders", "VSCodium"):
            candidate = mac_root / flavour / "User"
            if candidate.is_dir():
                found.append(candidate)

    # Linux: ~/.config/Code/User etc.
    linux_root = home / ".config"
    if linux_root.is_dir():
        for flavour in ("Code", "Code - Insiders", "VSCodium"):
            candidate = linux_root / flavour / "User"
            if candidate.is_dir():
                found.append(candidate)

    return found


def _default_jetbrains_dir() -> Optional[Path]:
    """Return the JetBrains config root if it exists on this machine."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "JetBrains"
        if candidate.is_dir():
            return candidate

    # macOS: ~/Library/Application Support/JetBrains
    home = Path.home()
    mac_candidate = home / "Library" / "Application Support" / "JetBrains"
    if mac_candidate.is_dir():
        return mac_candidate

    # Linux: ~/.config/JetBrains
    linux_candidate = home / ".config" / "JetBrains"
    if linux_candidate.is_dir():
        return linux_candidate

    return None


# ---------------------------------------------------------------------------
# VS Code workspace parsing
# ---------------------------------------------------------------------------


def _decode_vscode_uri(uri: str) -> str:
    """Convert a ``file:///`` URI to a plain filesystem path.

    VS Code stores ``file:///c%3A/Users/me/proj`` on Windows. Unquoting
    turns ``%3A`` back into ``:`` so the result is a usable path like
    ``c:/Users/me/proj``. Forward slashes are kept — Python's pathlib
    handles both separators on Windows.
    """
    if not uri:
        return ""
    if uri.startswith("file:///"):
        raw = uri[len("file:///") :]
    elif uri.startswith("file://"):
        raw = uri[len("file://") :]
    else:
        raw = uri
    return unquote(raw)


def _iter_vscode_workspaces(user_dir: Path) -> Iterator[Dict[str, Any]]:
    """Yield dicts describing each VS Code workspace under *user_dir*.

    Each dict has ``ide``, ``path``, ``last_opened``, ``source_file``,
    and ``flavour`` (the VS Code-family folder name).
    """
    ws_storage = user_dir / "workspaceStorage"
    if not ws_storage.is_dir():
        return

    # The parent of `User` is "Code", "Code - Insiders", "VSCodium" — use
    # that as the flavour label.
    flavour = user_dir.parent.name or "Code"

    try:
        subdirs = list(ws_storage.iterdir())
    except OSError:
        return

    for sub in subdirs:
        if not sub.is_dir():
            continue
        wjson = sub / "workspace.json"
        if not wjson.is_file():
            continue
        try:
            data = json.loads(wjson.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Single-folder workspaces have a `folder` key; multi-root /
        # named workspaces have a `workspace` key pointing at the
        # .code-workspace file.
        folder_uri = data.get("folder") or data.get("workspace") or ""
        if not folder_uri:
            continue

        try:
            mtime = datetime.fromtimestamp(sub.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = datetime.now(tz=timezone.utc)

        path = _decode_vscode_uri(folder_uri)
        # Doc-id key: hash subdir name + decoded path so it's stable across
        # syncs even if the workspaceStorage dir gets reorganized.
        yield {
            "ide": "vscode",
            "flavour": flavour,
            "path": path,
            "last_opened": mtime,
            "source_file": str(wjson),
            "key": sub.name,
        }


# ---------------------------------------------------------------------------
# JetBrains recent-project parsing
# ---------------------------------------------------------------------------


def _expand_jetbrains_path(raw: str) -> str:
    """Replace JetBrains macro variables with concrete filesystem paths."""
    if not raw:
        return ""
    expanded = raw.replace("$USER_HOME$", str(Path.home()))
    return expanded


def _iter_jetbrains_projects(root: Path) -> Iterator[Dict[str, Any]]:
    """Yield dicts describing each recent project from any JetBrains IDE under *root*.

    Walks every product dir (matching the typical ``<Product><Year>.<minor>``
    naming) and reads its ``options/recentProjects.xml``. Unknown product
    folders without that file are skipped silently.
    """
    try:
        product_dirs = list(root.iterdir())
    except OSError:
        return

    for product_dir in product_dirs:
        if not product_dir.is_dir():
            continue
        recent = product_dir / "options" / "recentProjects.xml"
        if not recent.is_file():
            continue

        try:
            tree = ET.parse(recent)
        except (ET.ParseError, OSError):
            continue

        root_el = tree.getroot()
        # The XML schema has shifted a couple of times. Modern JetBrains
        # uses ``<entry key="$USER_HOME$/path">`` wrapped under a
        # RecentProjectsManager component; older versions used a different
        # tree. ``iter("entry")`` plus per-entry option scanning handles
        # both.
        for entry in root_el.iter("entry"):
            key = entry.get("key", "")
            if not key:
                continue
            path = _expand_jetbrains_path(key)

            ts_value = ""
            for opt in entry.iter("option"):
                if opt.get("name") == "projectOpenTimestamp":
                    ts_value = opt.get("value", "")
                    break
            try:
                ts_ms = int(ts_value)
                last_opened = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                last_opened = datetime.now(tz=timezone.utc)

            yield {
                "ide": "jetbrains",
                "flavour": product_dir.name,
                "path": path,
                "last_opened": last_opened,
                "source_file": str(recent),
                "key": key,
            }


# ---------------------------------------------------------------------------
# IDEWorkspacesConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("ide_workspaces")
class IDEWorkspacesConnector(BaseConnector):
    """Connector that indexes recent VS Code and JetBrains project paths.

    Parameters
    ----------
    vscode_user_dirs:
        Override the auto-detected VS Code ``User`` directories. Pass a
        list of paths for tests or non-standard installs (e.g. portable
        Code). Empty list / ``None`` → auto-detect.
    jetbrains_dir:
        Override the auto-detected JetBrains config root. ``None`` →
        auto-detect.
    """

    connector_id = "ide_workspaces"
    display_name = "IDE Workspaces (VS Code + JetBrains)"
    auth_type = "filesystem"

    def __init__(
        self,
        vscode_user_dirs: Optional[List[str]] = None,
        jetbrains_dir: Optional[str] = None,
    ) -> None:
        if vscode_user_dirs is None:
            self._vscode_dirs: List[Path] = _default_vscode_user_dirs()
        else:
            self._vscode_dirs = [Path(p) for p in vscode_user_dirs if p]

        if jetbrains_dir is None:
            self._jetbrains_dir: Optional[Path] = _default_jetbrains_dir()
        elif jetbrains_dir == "":
            self._jetbrains_dir = None
        else:
            jb = Path(jetbrains_dir)
            self._jetbrains_dir = jb if jb.is_dir() else None

        self._items_synced: int = 0
        self._items_total: int = 0
        self._last_sync: Optional[datetime] = None

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """True if at least one VS Code User dir or the JetBrains dir exists."""
        has_vscode = any(p.is_dir() for p in self._vscode_dirs)
        has_jb = bool(self._jetbrains_dir and self._jetbrains_dir.is_dir())
        return has_vscode or has_jb

    def disconnect(self) -> None:
        self._vscode_dirs = []
        self._jetbrains_dir = None

    def sync(
        self,
        *,
        since: Optional[datetime] = None,
        cursor: Optional[str] = None,  # noqa: ARG002 — local read, no cursor
    ) -> Iterator[Document]:
        """Yield one :class:`Document` per recent workspace / project."""
        if not self.is_connected():
            return

        synced = 0
        seen_paths: set[str] = set()

        # ---- VS Code first ----
        for user_dir in self._vscode_dirs:
            if not user_dir.is_dir():
                continue
            for entry in _iter_vscode_workspaces(user_dir):
                doc = self._make_doc(entry, since=since)
                if doc is None:
                    continue
                # Deduplicate across stable/Insiders/Codium if the same
                # project happens to be open in multiple flavours — emit
                # the most recent one only.
                dedup_key = f"vscode|{entry['path'].lower()}"
                if dedup_key in seen_paths:
                    continue
                seen_paths.add(dedup_key)
                synced += 1
                yield doc

        # ---- JetBrains ----
        if self._jetbrains_dir and self._jetbrains_dir.is_dir():
            for entry in _iter_jetbrains_projects(self._jetbrains_dir):
                doc = self._make_doc(entry, since=since)
                if doc is None:
                    continue
                dedup_key = f"jetbrains|{entry['flavour']}|{entry['path'].lower()}"
                if dedup_key in seen_paths:
                    continue
                seen_paths.add(dedup_key)
                synced += 1
                yield doc

        self._items_synced = synced
        self._last_sync = datetime.now(tz=timezone.utc)

    def sync_status(self) -> SyncStatus:
        return SyncStatus(
            state="idle",
            items_synced=self._items_synced,
            last_sync=self._last_sync,
        )

    # ------------------------------------------------------------------
    # Internal: build a Document from a parsed-entry dict
    # ------------------------------------------------------------------

    def _make_doc(
        self,
        entry: Dict[str, Any],
        *,
        since: Optional[datetime],
    ) -> Optional[Document]:
        path: str = entry["path"] or ""
        if not path:
            return None
        last_opened: datetime = entry["last_opened"]

        if since is not None:
            since_aware = since
            if since.tzinfo is None and last_opened.tzinfo is not None:
                since_aware = since.replace(tzinfo=timezone.utc)
            if last_opened < since_aware:
                return None

        # Title = project's folder name; full path goes in the body.
        project_name = Path(path).name or path

        flavour = entry.get("flavour", "")
        ide = entry["ide"]
        ide_label = flavour if ide == "jetbrains" else flavour or "VS Code"

        content_lines: List[str] = [
            project_name,
            "",
            f"Path: {path}",
            f"IDE: {ide_label}",
            f"Last opened: {last_opened.isoformat()}",
        ]
        content = "\n".join(content_lines)

        doc_id_key = entry.get("key") or path
        return Document(
            doc_id=f"ide_workspaces:{ide}:{doc_id_key}",
            source="ide_workspaces",
            doc_type="workspace",
            content=content,
            title=project_name,
            timestamp=last_opened,
            metadata={
                "ide": ide,
                "flavour": flavour,
                "path": path,
                "source_file": entry.get("source_file", ""),
            },
        )
