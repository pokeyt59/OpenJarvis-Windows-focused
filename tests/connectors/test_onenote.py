"""Tests for OneNoteConnector — Microsoft Graph OneNote sync.

All Graph calls are mocked. No network access.
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

_LIST_RESPONSE = {
    "value": [
        {
            "id": "page-1",
            "title": "Project Kickoff",
            "createdDateTime": "2024-04-01T09:00:00Z",
            "lastModifiedDateTime": "2024-04-05T10:00:00Z",
            "links": {"oneNoteWebUrl": {"href": "https://onenote.com/page-1"}},
            "parentSection": {"displayName": "Work"},
        },
        {
            "id": "page-2",
            "title": "Travel ideas",
            "createdDateTime": "2024-04-02T09:00:00Z",
            "lastModifiedDateTime": "2024-04-06T11:00:00Z",
            "links": {"oneNoteWebUrl": {"href": "https://onenote.com/page-2"}},
            "parentSection": {"displayName": "Personal"},
        },
    ]
}

_PAGE_CONTENT_1 = (
    "<html><body>"
    "<h1>Project Kickoff</h1>"
    "<p>Discussed Q2 milestones.</p>"
    "<ul><li>Hire two engineers</li><li>Ship v1 of dashboard</li></ul>"
    "<script>console.log('noise');</script>"
    "</body></html>"
)

_PAGE_CONTENT_2 = (
    "<html><body>"
    "<p>Tokyo in fall, Lisbon in spring.</p>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def connector(tmp_path: Path):
    """OneNoteConnector pointed at a tmp credentials file (none on disk)."""
    from openjarvis.connectors.onenote import OneNoteConnector  # noqa: PLC0415

    creds_path = str(tmp_path / "onenote.json")
    return OneNoteConnector(credentials_path=creds_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_without_credentials(connector) -> None:
    """is_connected() is False when no credentials file exists."""
    assert connector.is_connected() is False


def test_connected_after_token_saved(connector, tmp_path: Path) -> None:
    """is_connected() flips to True once a real access_token is on disk."""
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token-1"}), encoding="utf-8"
    )
    assert connector.is_connected() is True


def test_auth_url_points_to_azure(connector) -> None:
    url = connector.auth_url()
    assert "portal.azure.com" in url


def test_handle_callback_with_raw_token(connector) -> None:
    """A bare access token is persisted as the legacy `token` key."""
    connector.handle_callback("ms-token-direct")
    raw = Path(connector._credentials_path).read_text(encoding="utf-8")
    assert "ms-token-direct" in raw


@patch("openjarvis.connectors.onenote._onenote_api_get_page_content")
@patch("openjarvis.connectors.onenote._onenote_api_list_pages")
def test_sync_yields_documents(
    mock_list,
    mock_content,
    connector,
) -> None:
    """sync() yields one Document per OneNote page with HTML stripped."""
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token-1"}), encoding="utf-8"
    )
    mock_list.return_value = _LIST_RESPONSE
    mock_content.side_effect = [_PAGE_CONTENT_1, _PAGE_CONTENT_2]

    docs: List[Document] = list(connector.sync())

    assert len(docs) == 2

    doc1 = next(d for d in docs if d.doc_id == "onenote:page-1")
    assert doc1.source == "onenote"
    assert doc1.doc_type == "note"
    assert doc1.title == "Project Kickoff"
    assert doc1.url == "https://onenote.com/page-1"
    assert doc1.metadata["section"] == "Work"
    # HTML stripped, list items kept on their own lines, script body dropped.
    assert "Discussed Q2 milestones." in doc1.content
    assert "Hire two engineers" in doc1.content
    assert "Ship v1 of dashboard" in doc1.content
    assert "console.log" not in doc1.content
    assert "<script>" not in doc1.content
    assert "<p>" not in doc1.content

    doc2 = next(d for d in docs if d.doc_id == "onenote:page-2")
    assert "Tokyo in fall" in doc2.content


@patch("openjarvis.connectors.onenote._onenote_api_get_page_content")
@patch("openjarvis.connectors.onenote._onenote_api_list_pages")
def test_sync_follows_next_link(
    mock_list,
    mock_content,
    connector,
) -> None:
    """When the response carries @odata.nextLink, sync paginates."""
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token-1"}), encoding="utf-8"
    )
    mock_list.side_effect = [
        {
            "value": [
                {
                    "id": "p1",
                    "title": "First",
                    "lastModifiedDateTime": "2024-04-01T00:00:00Z",
                    "links": {},
                    "parentSection": {},
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/cursor/abc",
        },
        {
            "value": [
                {
                    "id": "p2",
                    "title": "Second",
                    "lastModifiedDateTime": "2024-04-02T00:00:00Z",
                    "links": {},
                    "parentSection": {},
                }
            ]
        },
    ]
    mock_content.return_value = "<p>hi</p>"

    docs = list(connector.sync())
    assert {d.doc_id for d in docs} == {"onenote:p1", "onenote:p2"}
    # Second list call used the next_link cursor.
    _, second_kwargs = mock_list.call_args_list[1]
    assert second_kwargs["next_link"] == (
        "https://graph.microsoft.com/v1.0/cursor/abc"
    )


def test_html_to_text_strips_tags_and_unescapes() -> None:
    from openjarvis.connectors.onenote import _html_to_text  # noqa: PLC0415

    html = "<p>Hello &amp; goodbye</p><br><div>Next line</div>"
    text = _html_to_text(html)
    assert "Hello & goodbye" in text
    assert "Next line" in text
    assert "<" not in text


def test_html_to_text_drops_script_and_style() -> None:
    from openjarvis.connectors.onenote import _html_to_text  # noqa: PLC0415

    html = (
        "<style>p { color: red }</style>"
        "<p>Visible</p>"
        "<script>secret_token = 'abc'</script>"
    )
    text = _html_to_text(html)
    assert "Visible" in text
    assert "color: red" not in text
    assert "secret_token" not in text


def test_disconnect_removes_credentials(connector) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    assert connector.is_connected() is True
    connector.disconnect()
    assert connector.is_connected() is False
    assert not Path(connector._credentials_path).exists()


def test_mcp_tools(connector) -> None:
    tools = connector.mcp_tools()
    names = {t.name for t in tools}
    assert names == {"onenote_search_pages", "onenote_get_page"}


def test_registry() -> None:
    """OneNoteConnector registers under 'onenote' in ConnectorRegistry."""
    from openjarvis.connectors.onenote import OneNoteConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("onenote", OneNoteConnector)
    assert ConnectorRegistry.contains("onenote")
    cls = ConnectorRegistry.get("onenote")
    assert cls.connector_id == "onenote"
