"""Tests for OneDriveConnector — Microsoft Graph delta sync.

All Graph calls mocked. No network access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry


# ---------------------------------------------------------------------------
# Fake Graph payloads
# ---------------------------------------------------------------------------

_DELTA_RESPONSE = {
    "value": [
        # A folder — should be skipped.
        {
            "id": "folder-1",
            "name": "Documents",
            "folder": {"childCount": 3},
            "lastModifiedDateTime": "2024-04-01T09:00:00Z",
        },
        # A markdown file — downloadable text.
        {
            "id": "md-1",
            "name": "notes.md",
            "size": 42,
            "file": {"mimeType": "text/markdown"},
            "lastModifiedDateTime": "2024-04-05T10:00:00Z",
            "webUrl": "https://onedrive.live.com/notes.md",
            "parentReference": {"path": "/drive/root:/Documents"},
        },
        # An Office doc — should yield a metadata-only document.
        {
            "id": "docx-1",
            "name": "report.docx",
            "size": 12345,
            "file": {
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                )
            },
            "lastModifiedDateTime": "2024-04-06T11:00:00Z",
            "webUrl": "https://onedrive.live.com/report.docx",
            "parentReference": {"path": "/drive/root:/Documents"},
        },
        # A deleted item — should be skipped.
        {
            "id": "ghost-1",
            "name": "trashed.txt",
            "file": {"mimeType": "text/plain"},
            "deleted": {"state": "deleted"},
        },
    ],
    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/cursor/delta-xyz",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def connector(tmp_path: Path):
    from openjarvis.connectors.onedrive import OneDriveConnector  # noqa: PLC0415

    creds_path = str(tmp_path / "onedrive.json")
    return OneDriveConnector(credentials_path=creds_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_without_credentials(connector) -> None:
    assert connector.is_connected() is False


def test_connected_after_token_saved(connector) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    assert connector.is_connected() is True


def test_auth_url_points_to_azure(connector) -> None:
    assert "portal.azure.com" in connector.auth_url()


@patch("openjarvis.connectors.onedrive._onedrive_api_download")
@patch("openjarvis.connectors.onedrive._onedrive_api_delta")
def test_sync_skips_folders_and_deletes(
    mock_delta,
    mock_download,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_delta.return_value = _DELTA_RESPONSE
    mock_download.return_value = b"# Notes\n\nQ2 planning checklist"

    docs: List[Document] = list(connector.sync())

    # Folder + deleted item must not appear; only the .md and .docx remain.
    ids = {d.doc_id for d in docs}
    assert ids == {"onedrive:md-1", "onedrive:docx-1"}


@patch("openjarvis.connectors.onedrive._onedrive_api_download")
@patch("openjarvis.connectors.onedrive._onedrive_api_delta")
def test_sync_downloads_text_files(
    mock_delta,
    mock_download,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_delta.return_value = _DELTA_RESPONSE
    mock_download.return_value = b"# Notes\n\nQ2 planning checklist"

    docs = list(connector.sync())

    md_doc = next(d for d in docs if d.doc_id == "onedrive:md-1")
    assert md_doc.title == "notes.md"
    assert md_doc.source == "onedrive"
    assert md_doc.doc_type == "file"
    assert "Q2 planning checklist" in md_doc.content
    assert md_doc.url == "https://onedrive.live.com/notes.md"
    assert md_doc.metadata["mime_type"] == "text/markdown"
    assert md_doc.metadata["size_bytes"] == 42

    # Download was called exactly once (only for the markdown file).
    assert mock_download.call_count == 1


@patch("openjarvis.connectors.onedrive._onedrive_api_download")
@patch("openjarvis.connectors.onedrive._onedrive_api_delta")
def test_sync_metadata_only_for_office_docs(
    mock_delta,
    mock_download,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_delta.return_value = _DELTA_RESPONSE
    mock_download.return_value = b"hello"

    docs = list(connector.sync())

    docx = next(d for d in docs if d.doc_id == "onedrive:docx-1")
    # Body is a [File: ...] placeholder — Office docs aren't downloaded.
    assert "report.docx" in docx.content
    assert docx.content.startswith("[File:")


@patch("openjarvis.connectors.onedrive._onedrive_api_download")
@patch("openjarvis.connectors.onedrive._onedrive_api_delta")
def test_sync_persists_delta_link_as_cursor(
    mock_delta,
    mock_download,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_delta.return_value = _DELTA_RESPONSE
    mock_download.return_value = b"data"

    list(connector.sync())
    status = connector.sync_status()
    assert status.cursor == "https://graph.microsoft.com/v1.0/cursor/delta-xyz"


@patch("openjarvis.connectors.onedrive._onedrive_api_download")
@patch("openjarvis.connectors.onedrive._onedrive_api_delta")
def test_sync_follows_next_link_then_delta(
    mock_delta,
    mock_download,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_delta.side_effect = [
        {
            "value": [
                {
                    "id": "f1",
                    "name": "a.md",
                    "size": 5,
                    "file": {"mimeType": "text/markdown"},
                    "lastModifiedDateTime": "2024-04-01T00:00:00Z",
                    "webUrl": "https://x/a.md",
                    "parentReference": {},
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/page/2",
        },
        {
            "value": [
                {
                    "id": "f2",
                    "name": "b.md",
                    "size": 5,
                    "file": {"mimeType": "text/markdown"},
                    "lastModifiedDateTime": "2024-04-02T00:00:00Z",
                    "webUrl": "https://x/b.md",
                    "parentReference": {},
                }
            ],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/cursor/end",
        },
    ]
    mock_download.return_value = b"hello"

    docs = list(connector.sync())
    assert {d.doc_id for d in docs} == {"onedrive:f1", "onedrive:f2"}
    assert connector.sync_status().cursor == (
        "https://graph.microsoft.com/v1.0/cursor/end"
    )


def test_is_text_file_by_mime() -> None:
    from openjarvis.connectors.onedrive import _is_text_file  # noqa: PLC0415

    assert _is_text_file("anything", "text/plain")
    assert _is_text_file("anything", "text/markdown")
    assert _is_text_file("anything", "application/json")
    assert not _is_text_file(
        "binary.bin",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def test_is_text_file_by_extension() -> None:
    from openjarvis.connectors.onedrive import _is_text_file  # noqa: PLC0415

    # Even when Graph reports octet-stream, the extension wins.
    assert _is_text_file("notes.md", "application/octet-stream")
    assert _is_text_file("data.csv", "")
    assert _is_text_file("script.py", "application/octet-stream")
    assert not _is_text_file("photo.png", "image/png")
    assert not _is_text_file("doc.pdf", "application/pdf")


def test_disconnect(connector) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    connector.disconnect()
    assert not Path(connector._credentials_path).exists()


def test_mcp_tools(connector) -> None:
    tools = connector.mcp_tools()
    names = {t.name for t in tools}
    assert "onedrive_search_files" in names


def test_registry() -> None:
    from openjarvis.connectors.onedrive import OneDriveConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("onedrive", OneDriveConnector)
    assert ConnectorRegistry.contains("onedrive")
    cls = ConnectorRegistry.get("onedrive")
    assert cls.connector_id == "onedrive"
