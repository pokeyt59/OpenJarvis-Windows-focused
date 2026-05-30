"""Tests for the ConfigFile primitive."""

from __future__ import annotations

from pathlib import Path

import pytest

from openjarvis.installers.base import (
    StepStatus,
    StorageKind,
    Wipeability,
)
from openjarvis.installers.primitives.files import ConfigFile


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "subdir" / "settings.yml"


class TestConfigFile:
    def test_detect_not_installed_when_missing(self, cfg_path: Path) -> None:
        cf = ConfigFile(path=cfg_path, content="hello", item_id="x")
        assert cf.detect() == StepStatus.NOT_INSTALLED

    def test_detect_installed_when_content_matches(self, cfg_path: Path) -> None:
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("hello")
        cf = ConfigFile(path=cfg_path, content="hello", item_id="x")
        assert cf.detect() == StepStatus.INSTALLED

    def test_detect_partial_when_content_differs(self, cfg_path: Path) -> None:
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("OLD")
        cf = ConfigFile(path=cfg_path, content="NEW", item_id="x")
        assert cf.detect() == StepStatus.PARTIAL

    def test_install_writes_file(self, cfg_path: Path) -> None:
        cf = ConfigFile(path=cfg_path, content="hello", item_id="x")
        events = list(cf.install())
        assert cfg_path.read_text() == "hello"
        assert events[-1].percent == 100.0

    def test_install_overwrites_when_content_differs(self, cfg_path: Path) -> None:
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("OLD")
        cf = ConfigFile(path=cfg_path, content="NEW", item_id="x")
        list(cf.install())
        assert cfg_path.read_text() == "NEW"

    def test_install_noop_when_already_matching(self, cfg_path: Path) -> None:
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("hello")
        mtime_before = cfg_path.stat().st_mtime_ns
        cf = ConfigFile(path=cfg_path, content="hello", item_id="x")
        events = list(cf.install())
        # File should not have been rewritten.
        assert cfg_path.stat().st_mtime_ns == mtime_before
        assert events[-1].percent == 100.0

    def test_verify_returns_true_when_present(self, cfg_path: Path) -> None:
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("hello")
        cf = ConfigFile(path=cfg_path, content="hello", item_id="x")
        assert cf.verify() is True

    def test_uninstall_is_noop(self, cfg_path: Path) -> None:
        """Uninstall must leave the file alone — it may contain user edits."""
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("user edited")
        cf = ConfigFile(path=cfg_path, content="default", item_id="x")
        cf.uninstall()
        assert cfg_path.exists()
        assert cfg_path.read_text() == "user edited"

    def test_storage_inventory_reports_replaceable_config(
        self, cfg_path: Path
    ) -> None:
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("xxxxxxxx")  # 8 bytes
        cf = ConfigFile(
            path=cfg_path,
            content="xxxxxxxx",
            item_id="my.config",
            description="my fancy config",
        )
        items = cf.storage_inventory()
        assert len(items) == 1
        item = items[0]
        assert item.item_id == "my.config"
        assert item.kind == StorageKind.CONFIG
        assert item.wipeability == Wipeability.REPLACEABLE
        assert item.size_bytes == 8
        assert item.description == "my fancy config"
