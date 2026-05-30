"""Tests for the deep-research-setup CLI command.

After the macOS removal, the only `detect_local_sources` source is Obsidian.
Apple Notes / iMessage detection tests were dropped along with their
connector modules. Token-based detection tests stay.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# detect_local_sources (Obsidian only after the macOS removal)
# ---------------------------------------------------------------------------


def test_detect_local_sources_no_obsidian_returns_empty(tmp_path: Path) -> None:
    """No vault path → no local sources."""
    from openjarvis.cli.deep_research_setup_cmd import detect_local_sources

    assert detect_local_sources(obsidian_vault_path=None) == []


def test_detect_includes_obsidian_when_vault_exists(tmp_path: Path) -> None:
    """Auto-detection includes Obsidian when vault path exists."""
    from openjarvis.cli.deep_research_setup_cmd import detect_local_sources

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Hello")

    sources = detect_local_sources(obsidian_vault_path=vault)
    ids = [s["connector_id"] for s in sources]
    assert "obsidian" in ids


def test_detect_skips_obsidian_when_vault_missing(tmp_path: Path) -> None:
    """Auto-detection skips Obsidian when the path doesn't exist."""
    from openjarvis.cli.deep_research_setup_cmd import detect_local_sources

    sources = detect_local_sources(obsidian_vault_path=tmp_path / "nope")
    assert sources == []


def test_ingest_sources(tmp_path: Path) -> None:
    """ingest_sources connects and ingests documents into KnowledgeStore."""
    from openjarvis.cli.deep_research_setup_cmd import (
        detect_local_sources,
        ingest_sources,
    )
    from openjarvis.connectors.store import KnowledgeStore

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Hello world\n\nThis is a test note.")

    sources = detect_local_sources(obsidian_vault_path=vault)

    db_path = tmp_path / "knowledge.db"
    state_db = str(tmp_path / "sync_state.db")
    store = KnowledgeStore(str(db_path))
    total = ingest_sources(sources, store, state_db=state_db)

    assert total > 0
    assert store.count() > 0
    store.close()


# ---------------------------------------------------------------------------
# detect_token_sources
# ---------------------------------------------------------------------------


def test_detect_token_sources_finds_connected(tmp_path: Path) -> None:
    """detect_token_sources finds sources with valid credential files."""
    from openjarvis.cli.deep_research_setup_cmd import detect_token_sources

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "slack.json").write_text('{"token": "xoxb-test"}')
    (connectors_dir / "notion.json").write_text('{"token": "ntn_test"}')

    sources = detect_token_sources(connectors_dir=connectors_dir)
    ids = [s["connector_id"] for s in sources]
    assert "slack" in ids
    assert "notion" in ids


def test_detect_token_sources_skips_empty(tmp_path: Path) -> None:
    """detect_token_sources skips files with empty or invalid JSON."""
    from openjarvis.cli.deep_research_setup_cmd import detect_token_sources

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "slack.json").write_text("{}")
    (connectors_dir / "notion.json").write_text("invalid json")

    sources = detect_token_sources(connectors_dir=connectors_dir)
    assert len(sources) == 0


def test_detect_token_sources_empty_dir(tmp_path: Path) -> None:
    """detect_token_sources returns empty list when no credential files exist."""
    from openjarvis.cli.deep_research_setup_cmd import detect_token_sources

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    sources = detect_token_sources(connectors_dir=connectors_dir)
    assert len(sources) == 0
