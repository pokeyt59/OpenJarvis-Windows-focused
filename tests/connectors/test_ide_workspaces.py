"""Tests for IDEWorkspacesConnector — recent VS Code + JetBrains projects.

Builds fake VS Code workspaceStorage and JetBrains recentProjects.xml
trees inside tmp_path so the tests run without any editor installed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vscode_workspace(
    user_dir: Path,
    *,
    subdir: str,
    folder_uri: str,
    mtime: datetime,
) -> None:
    """Drop a workspace.json under user_dir/workspaceStorage/<subdir>/."""
    ws = user_dir / "workspaceStorage" / subdir
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": folder_uri}), encoding="utf-8"
    )
    # Force the directory mtime so the connector picks up a deterministic
    # last-touched time.
    ts = mtime.timestamp()
    os.utime(ws, (ts, ts))


def _make_jetbrains_recent(
    jetbrains_dir: Path,
    *,
    product: str,
    entries: List[dict],
) -> None:
    """Write a minimal recentProjects.xml for *product* under jetbrains_dir.

    Each entry is ``{"path": "$USER_HOME$/dev/foo", "ts_ms": 1712310000000}``.
    """
    options = jetbrains_dir / product / "options"
    options.mkdir(parents=True, exist_ok=True)
    xml_lines = ["<application>", '  <component name="RecentProjectsManager">']
    for e in entries:
        xml_lines.append(
            f'    <entry key="{e["path"]}">'
        )
        xml_lines.append('      <value>')
        xml_lines.append('        <RecentProjectMetaInfo>')
        xml_lines.append(
            f'          <option name="projectOpenTimestamp" value="{e["ts_ms"]}" />'
        )
        xml_lines.append('        </RecentProjectMetaInfo>')
        xml_lines.append('      </value>')
        xml_lines.append('    </entry>')
    xml_lines.append("  </component>")
    xml_lines.append("</application>")
    (options / "recentProjects.xml").write_text(
        "\n".join(xml_lines), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vscode_user_dir(tmp_path: Path) -> Path:
    """Fake VS Code stable User directory with two workspaces."""
    user_dir = tmp_path / "Code" / "User"
    user_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    _make_vscode_workspace(
        user_dir,
        subdir="abc123",
        folder_uri="file:///d%3A/Git/OpenJarvis",
        mtime=now - timedelta(hours=2),
    )
    _make_vscode_workspace(
        user_dir,
        subdir="def456",
        folder_uri="file:///c%3A/Users/me/projects/dotfiles",
        mtime=now - timedelta(days=5),
    )
    return user_dir


@pytest.fixture()
def jetbrains_dir(tmp_path: Path) -> Path:
    """Fake JetBrains config root with two product dirs."""
    jb = tmp_path / "JetBrains"
    jb.mkdir()

    base_ms = int(datetime(2024, 4, 5, tzinfo=timezone.utc).timestamp() * 1000)
    _make_jetbrains_recent(
        jb,
        product="IntelliJIdea2024.1",
        entries=[
            {"path": "$USER_HOME$/dev/server", "ts_ms": base_ms},
            {"path": "$USER_HOME$/dev/cli-tool", "ts_ms": base_ms - 86_400_000},
        ],
    )
    _make_jetbrains_recent(
        jb,
        product="WebStorm2024.1",
        entries=[
            {"path": "$USER_HOME$/dev/web-app", "ts_ms": base_ms + 3_600_000},
        ],
    )
    return jb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_not_connected_without_any_dirs() -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    # Explicit empty overrides → connector has nothing to read.
    conn = IDEWorkspacesConnector(vscode_user_dirs=[], jetbrains_dir="")
    assert conn.is_connected() is False


def test_connected_with_vscode_only(vscode_user_dir: Path) -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(vscode_user_dir)],
        jetbrains_dir="",
    )
    assert conn.is_connected() is True


def test_connected_with_jetbrains_only(jetbrains_dir: Path) -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[],
        jetbrains_dir=str(jetbrains_dir),
    )
    assert conn.is_connected() is True


def test_sync_yields_vscode_workspaces(vscode_user_dir: Path) -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(vscode_user_dir)],
        jetbrains_dir="",
    )
    docs: List[Document] = list(conn.sync())

    titles = {d.title for d in docs}
    assert "OpenJarvis" in titles
    assert "dotfiles" in titles

    # Path decoding: the URI-encoded ":" should have been unwrapped.
    open_jarvis = next(d for d in docs if d.title == "OpenJarvis")
    assert open_jarvis.source == "ide_workspaces"
    assert open_jarvis.doc_type == "workspace"
    assert open_jarvis.metadata["ide"] == "vscode"
    assert "d:" in open_jarvis.metadata["path"].lower()
    assert open_jarvis.metadata["flavour"] == "Code"


def test_sync_yields_jetbrains_projects(jetbrains_dir: Path) -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[],
        jetbrains_dir=str(jetbrains_dir),
    )
    docs = list(conn.sync())
    titles = {d.title for d in docs}
    assert {"server", "cli-tool", "web-app"} <= titles

    web = next(d for d in docs if d.title == "web-app")
    assert web.metadata["ide"] == "jetbrains"
    assert web.metadata["flavour"] == "WebStorm2024.1"
    # $USER_HOME$ was expanded.
    assert "$USER_HOME$" not in web.metadata["path"]


def test_sync_combines_both_sources(vscode_user_dir: Path, jetbrains_dir: Path) -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(vscode_user_dir)],
        jetbrains_dir=str(jetbrains_dir),
    )
    docs = list(conn.sync())
    titles = {d.title for d in docs}
    assert {"OpenJarvis", "dotfiles", "server", "cli-tool", "web-app"} <= titles


def test_sync_respects_since(jetbrains_dir: Path) -> None:
    """Projects opened before the cutoff are filtered out."""
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[],
        jetbrains_dir=str(jetbrains_dir),
    )
    cutoff = datetime(2024, 4, 5, tzinfo=timezone.utc)
    docs = list(conn.sync(since=cutoff))
    titles = {d.title for d in docs}
    # web-app (4-05 +1h) and server (4-05) should remain;
    # cli-tool (4-04) should be filtered out.
    assert "web-app" in titles
    assert "cli-tool" not in titles


def test_sync_dedupes_same_path_across_vscode_flavours(tmp_path: Path) -> None:
    """The same project opened in stable + Insiders only yields one doc."""
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    code_user = tmp_path / "Code" / "User"
    insiders_user = tmp_path / "Code - Insiders" / "User"
    code_user.mkdir(parents=True)
    insiders_user.mkdir(parents=True)

    same_uri = "file:///d%3A/Git/SharedProject"
    now = datetime.now(timezone.utc)
    _make_vscode_workspace(code_user, subdir="a", folder_uri=same_uri, mtime=now)
    _make_vscode_workspace(
        insiders_user, subdir="b", folder_uri=same_uri, mtime=now - timedelta(days=1)
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(code_user), str(insiders_user)],
        jetbrains_dir="",
    )
    docs = list(conn.sync())
    titles = [d.title for d in docs]
    assert titles.count("SharedProject") == 1


def test_sync_handles_missing_workspace_storage(tmp_path: Path) -> None:
    """A User dir without workspaceStorage doesn't crash."""
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True)
    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(user)],
        jetbrains_dir="",
    )
    docs = list(conn.sync())
    assert docs == []


def test_sync_handles_malformed_workspace_json(tmp_path: Path) -> None:
    """A subdir whose workspace.json is invalid JSON is skipped, not fatal."""
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    user = tmp_path / "Code" / "User"
    ws = user / "workspaceStorage" / "broken"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text("{ this is not json", encoding="utf-8")

    # Plus a valid one alongside.
    _make_vscode_workspace(
        user,
        subdir="ok",
        folder_uri="file:///d%3A/Git/Healthy",
        mtime=datetime.now(timezone.utc),
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(user)],
        jetbrains_dir="",
    )
    titles = {d.title for d in conn.sync()}
    assert titles == {"Healthy"}


def test_sync_handles_malformed_jetbrains_xml(tmp_path: Path) -> None:
    """A product dir whose XML is broken is skipped, not fatal."""
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    jb = tmp_path / "JetBrains"
    broken_opts = jb / "PyCharm2024.1" / "options"
    broken_opts.mkdir(parents=True)
    (broken_opts / "recentProjects.xml").write_text(
        "<not valid xml<<<", encoding="utf-8"
    )

    # Plus a valid one.
    _make_jetbrains_recent(
        jb,
        product="GoLand2024.1",
        entries=[{"path": "$USER_HOME$/dev/api", "ts_ms": 1712310000000}],
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[],
        jetbrains_dir=str(jb),
    )
    titles = {d.title for d in conn.sync()}
    assert titles == {"api"}


def test_disconnect(vscode_user_dir: Path, jetbrains_dir: Path) -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    conn = IDEWorkspacesConnector(
        vscode_user_dirs=[str(vscode_user_dir)],
        jetbrains_dir=str(jetbrains_dir),
    )
    assert conn.is_connected() is True
    conn.disconnect()
    assert conn.is_connected() is False


def test_decode_vscode_uri() -> None:
    from openjarvis.connectors.ide_workspaces import _decode_vscode_uri  # noqa: PLC0415

    assert _decode_vscode_uri("file:///c%3A/Users/foo") == "c:/Users/foo"
    assert _decode_vscode_uri("file:///home/foo/proj") == "home/foo/proj"
    assert _decode_vscode_uri("") == ""


def test_expand_jetbrains_path() -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        _expand_jetbrains_path,
    )

    home = str(Path.home())
    assert _expand_jetbrains_path("$USER_HOME$/dev/foo") == f"{home}/dev/foo"
    # Plain paths pass through unchanged.
    assert _expand_jetbrains_path("d:/Git/Proj") == "d:/Git/Proj"


def test_registry() -> None:
    from openjarvis.connectors.ide_workspaces import (  # noqa: PLC0415
        IDEWorkspacesConnector,
    )

    ConnectorRegistry.register_value("ide_workspaces", IDEWorkspacesConnector)
    assert ConnectorRegistry.contains("ide_workspaces")
    cls = ConnectorRegistry.get("ide_workspaces")
    assert cls.connector_id == "ide_workspaces"
