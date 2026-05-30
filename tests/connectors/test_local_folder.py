"""Tests for LocalFolderConnector — generic local folder walker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_folder(tmp_path: Path) -> Path:
    """Create a folder tree with a mix of file types for the connector to see."""
    # Text files at root
    (tmp_path / "readme.md").write_text("# Hello\n\nWelcome", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Random thoughts", encoding="utf-8")
    (tmp_path / "data.csv").write_text("a,b,c\n1,2,3", encoding="utf-8")

    # A binary file that should be ignored.
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\x00garbage")

    # A nested folder with a python file
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def main():\n    print('hi')", encoding="utf-8"
    )

    # A junk directory that must be pruned
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.md").write_text("nope", encoding="utf-8")

    # A hidden directory that must be pruned
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("not indexed", encoding="utf-8")

    # Windows-specific junk files
    (tmp_path / "Thumbs.db").write_text("nope", encoding="utf-8")
    (tmp_path / "desktop.ini").write_text("nope", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_without_path() -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector()
    assert conn.is_connected() is False


def test_not_connected_with_missing_path(tmp_path: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    bogus = str(tmp_path / "does-not-exist")
    conn = LocalFolderConnector(folder_path=bogus)
    assert conn.is_connected() is False


def test_connected_when_folder_exists(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    assert conn.is_connected() is True


def test_sync_indexes_text_files_only(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    docs: List[Document] = list(conn.sync())

    titles = {d.title for d in docs}
    # The four text files we created — and nothing else.
    assert titles == {"readme", "notes", "data", "main"}


def test_sync_skips_junk_directories(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    docs = list(conn.sync())

    # Nothing from .git or node_modules should appear.
    for doc in docs:
        assert "node_modules" not in doc.metadata["relative_path"]
        assert ".git" not in doc.metadata["relative_path"]


def test_sync_skips_thumbs_and_desktop_ini(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    docs = list(conn.sync())
    titles = {d.title for d in docs}
    assert "Thumbs" not in titles
    assert "desktop" not in titles


def test_sync_preserves_content(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    docs = list(conn.sync())
    readme = next(d for d in docs if d.title == "readme")
    assert "Hello" in readme.content
    assert "Welcome" in readme.content
    assert readme.source == "local_folder"
    assert readme.doc_type == "file"
    assert readme.metadata["extension"] == ".md"


def test_sync_doc_id_is_relative_path(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    docs = list(conn.sync())
    main_doc = next(d for d in docs if d.title == "main")
    # On Windows the separator is backslash; the assertion is just that the
    # subdirectory is present in the doc_id and the value starts with the
    # connector prefix.
    assert main_doc.doc_id.startswith("local_folder:")
    assert "src" in main_doc.doc_id


def test_sync_skips_oversized_files(tmp_path: Path) -> None:
    """Files larger than _MAX_FILE_BYTES are silently skipped."""
    from openjarvis.connectors import local_folder  # noqa: PLC0415
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    big = tmp_path / "huge.txt"
    # 3 MB body — 1 MB above the 2 MB ceiling.
    big.write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
    small = tmp_path / "ok.md"
    small.write_text("small content", encoding="utf-8")

    conn = LocalFolderConnector(folder_path=str(tmp_path))
    titles = {d.title for d in conn.sync()}
    assert "ok" in titles
    assert "huge" not in titles
    # Sanity-check the constant is what we expect so future tweaks don't
    # silently invalidate the test.
    assert local_folder._MAX_FILE_BYTES == 2 * 1024 * 1024


def test_sync_respects_since(populated_folder: Path) -> None:
    """Files older than *since* are skipped."""
    import os
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    # Force one file far into the past.
    old_file = populated_folder / "old.md"
    old_file.write_text("ancient", encoding="utf-8")
    past = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old_file, (past, past))

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    titles = {d.title for d in conn.sync(since=cutoff)}
    assert "old" not in titles
    # The newly-written readme/notes/etc. should still be included.
    assert "readme" in titles


def test_disconnect(populated_folder: Path) -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    conn = LocalFolderConnector(folder_path=str(populated_folder))
    assert conn.is_connected() is True
    conn.disconnect()
    assert conn.is_connected() is False


def test_registry() -> None:
    from openjarvis.connectors.local_folder import LocalFolderConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("local_folder", LocalFolderConnector)
    assert ConnectorRegistry.contains("local_folder")
    cls = ConnectorRegistry.get("local_folder")
    assert cls.connector_id == "local_folder"
