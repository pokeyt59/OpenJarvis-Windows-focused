"""Local folder connector — index any directory on disk.

Generalization of :mod:`openjarvis.connectors.obsidian`. Where Obsidian
assumes a vault layout (``.obsidian`` directory, YAML frontmatter), this
connector handles arbitrary user-chosen folders: Desktop, Documents, a
project directory, anywhere you have read access. It walks recursively,
filters by extension, and yields one :class:`Document` per file.

Designed for "I have a pile of notes / PDFs / source code on disk that
I want OpenJarvis to know about" — Windows-friendly defaults (it skips
``$Recycle.Bin``, ``System Volume Information``, ``Thumbs.db`` and the
usual ``__pycache__`` / ``node_modules`` noise) but cross-platform too.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.registry import ConnectorRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extensions that we read as plain text. We deliberately err on the side of
# "include it" because the downstream chunker handles weird encodings, and
# the alternative (silent skip) is much harder to debug than "indexed but
# garbled."
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
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
    ".conf",
    ".env",
    ".sql",
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
    ".sh",
    ".bat",
    ".ps1",
}

# Directory names we never recurse into. Windows-specific entries first
# because this connector's primary audience is Windows; the rest cover
# cross-platform junk that should never be indexed.
_SKIP_DIRS = {
    # Windows system directories
    "$Recycle.Bin",
    "System Volume Information",
    "$RECYCLE.BIN",
    # Hidden / cache directories
    ".git",
    ".hg",
    ".svn",
    ".obsidian",
    ".trash",
    ".cache",
    ".vscode",
    ".idea",
    "__pycache__",
    # Dependency / build output dirs
    "node_modules",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "target",
    ".next",
}

# Files we always skip even if their extension is in _TEXT_EXTENSIONS.
_SKIP_FILES = {
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
}

# Files larger than this are skipped — they're usually generated or noisy
# (massive logs, JSON dumps, etc.) and not worth blowing up the index.
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB


# ---------------------------------------------------------------------------
# LocalFolderConnector
# ---------------------------------------------------------------------------


@ConnectorRegistry.register("local_folder")
class LocalFolderConnector(BaseConnector):
    """Index a user-chosen folder recursively for text files.

    Parameters
    ----------
    folder_path:
        Absolute path to the folder to index. An empty string means
        "not yet configured" — :meth:`is_connected` returns ``False``.
    """

    connector_id = "local_folder"
    display_name = "Local Folder"
    auth_type = "filesystem"

    def __init__(self, folder_path: str = "") -> None:
        self._folder_path: str = folder_path
        self._items_synced: int = 0
        self._items_total: int = 0
        self._last_sync: Optional[datetime] = None

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        return bool(self._folder_path) and Path(self._folder_path).is_dir()

    def disconnect(self) -> None:
        self._folder_path = ""

    def sync(
        self,
        *,
        since: Optional[datetime] = None,
        cursor: Optional[str] = None,  # noqa: ARG002 — unused, filesystem walk
    ) -> Iterator[Document]:
        """Walk the folder recursively and yield a :class:`Document` per file."""
        if not self.is_connected():
            return

        root = Path(self._folder_path)
        root_name = root.name or str(root)

        collected: List[Path] = []
        for dir_path, dirs, files in os.walk(root):
            # Prune in-place so os.walk doesn't descend into junk dirs.
            dirs[:] = [
                d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for fname in files:
                if fname in _SKIP_FILES:
                    continue
                fpath = Path(dir_path) / fname
                if fpath.suffix.lower() not in _TEXT_EXTENSIONS:
                    continue
                collected.append(fpath)

        self._items_total = len(collected)
        synced = 0

        for fpath in collected:
            try:
                stat = fpath.stat()
            except OSError:
                continue

            if stat.st_size > _MAX_FILE_BYTES:
                continue

            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if since is not None:
                # Compare with timezone-aware fallback.
                since_aware = since
                if since.tzinfo is None:
                    since_aware = since.replace(tzinfo=timezone.utc)
                if mtime < since_aware:
                    continue

            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue

            try:
                rel_path = fpath.relative_to(root)
            except ValueError:
                # Shouldn't happen since fpath came from os.walk(root), but
                # be defensive — fall back to absolute path display.
                rel_path = fpath

            doc = Document(
                doc_id=f"local_folder:{rel_path}",
                source="local_folder",
                doc_type="file",
                content=text,
                title=fpath.stem,
                timestamp=mtime,
                metadata={
                    "root": str(root),
                    "root_name": root_name,
                    "relative_path": str(rel_path),
                    "size_bytes": stat.st_size,
                    "extension": fpath.suffix.lower(),
                },
            )
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
