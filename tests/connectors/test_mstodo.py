"""Tests for MSToDoConnector — Microsoft To Do via Graph.

All Graph calls mocked.
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

_LISTS_RESPONSE = {
    "value": [
        {"id": "list-personal", "displayName": "Personal", "isOwner": True},
        {"id": "list-work", "displayName": "Work", "isOwner": True},
    ]
}

_TASKS_PERSONAL = {
    "value": [
        {
            "id": "t-1",
            "title": "Buy groceries",
            "status": "notStarted",
            "importance": "normal",
            "lastModifiedDateTime": "2024-04-05T10:00:00Z",
            "body": {"content": "Milk, eggs, bread", "contentType": "text"},
            "dueDateTime": {"dateTime": "2024-04-07T00:00:00", "timeZone": "UTC"},
        },
        {
            "id": "t-2",
            "title": "Renew passport",
            "status": "inProgress",
            "importance": "high",
            "lastModifiedDateTime": "2024-04-04T10:00:00Z",
            "body": {"content": "", "contentType": "text"},
        },
    ]
}

_TASKS_WORK = {
    "value": [
        {
            "id": "t-3",
            "title": "Send quarterly report",
            "status": "completed",
            "importance": "normal",
            "lastModifiedDateTime": "2024-04-03T10:00:00Z",
            "body": {"content": "Email to leadership", "contentType": "text"},
        },
    ]
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def connector(tmp_path: Path):
    from openjarvis.connectors.mstodo import MSToDoConnector  # noqa: PLC0415

    creds_path = str(tmp_path / "mstodo.json")
    return MSToDoConnector(credentials_path=creds_path)


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


@patch("openjarvis.connectors.mstodo._mstodo_api_list_tasks")
@patch("openjarvis.connectors.mstodo._mstodo_api_list_lists")
def test_sync_walks_every_list(
    mock_lists,
    mock_tasks,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_lists.return_value = _LISTS_RESPONSE
    mock_tasks.side_effect = [_TASKS_PERSONAL, _TASKS_WORK]

    docs: List[Document] = list(connector.sync())

    assert len(docs) == 3
    ids = {d.doc_id for d in docs}
    assert ids == {"mstodo:t-1", "mstodo:t-2", "mstodo:t-3"}

    # Per-task assertions
    buy = next(d for d in docs if d.doc_id == "mstodo:t-1")
    assert buy.title == "Buy groceries"
    assert buy.source == "mstodo"
    assert buy.doc_type == "task"
    assert buy.channel == "Personal"
    assert "Milk, eggs, bread" in buy.content
    assert "Status: notStarted" in buy.content
    assert "List: Personal" in buy.content
    assert buy.metadata["list_name"] == "Personal"
    assert buy.metadata["due"] == "2024-04-07T00:00:00"

    passport = next(d for d in docs if d.doc_id == "mstodo:t-2")
    # High importance gets surfaced in the body footer.
    assert "Importance: high" in passport.content

    report = next(d for d in docs if d.doc_id == "mstodo:t-3")
    assert report.channel == "Work"
    assert report.metadata["status"] == "completed"


@patch("openjarvis.connectors.mstodo._mstodo_api_list_tasks")
@patch("openjarvis.connectors.mstodo._mstodo_api_list_lists")
def test_sync_paginates_tasks_with_next_link(
    mock_lists,
    mock_tasks,
    connector,
) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    mock_lists.return_value = {
        "value": [{"id": "L1", "displayName": "All"}]
    }
    mock_tasks.side_effect = [
        {
            "value": [
                {
                    "id": "t-a",
                    "title": "Task A",
                    "lastModifiedDateTime": "2024-04-01T00:00:00Z",
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/cursor/page-2",
        },
        {
            "value": [
                {
                    "id": "t-b",
                    "title": "Task B",
                    "lastModifiedDateTime": "2024-04-02T00:00:00Z",
                }
            ]
        },
    ]

    docs = list(connector.sync())
    assert {d.doc_id for d in docs} == {"mstodo:t-a", "mstodo:t-b"}
    # Second tasks call passed the next_link from the first response.
    _, second_kwargs = mock_tasks.call_args_list[1]
    assert second_kwargs["next_link"] == (
        "https://graph.microsoft.com/v1.0/cursor/page-2"
    )


def test_format_task_body_minimal_task() -> None:
    """A task with only a title still renders cleanly."""
    from openjarvis.connectors.mstodo import _format_task_body  # noqa: PLC0415

    rendered = _format_task_body({"title": "Standalone"}, "Inbox")
    assert "Standalone" in rendered
    assert "List: Inbox" in rendered


def test_disconnect(connector) -> None:
    Path(connector._credentials_path).write_text(
        json.dumps({"access_token": "ms-token"}), encoding="utf-8"
    )
    connector.disconnect()
    assert not Path(connector._credentials_path).exists()


def test_mcp_tools(connector) -> None:
    tools = connector.mcp_tools()
    names = {t.name for t in tools}
    assert "mstodo_search_tasks" in names


def test_registry() -> None:
    from openjarvis.connectors.mstodo import MSToDoConnector  # noqa: PLC0415

    ConnectorRegistry.register_value("mstodo", MSToDoConnector)
    assert ConnectorRegistry.contains("mstodo")
    cls = ConnectorRegistry.get("mstodo")
    assert cls.connector_id == "mstodo"
