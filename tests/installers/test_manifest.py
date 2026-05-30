"""Tests for the image manifest helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjarvis.installers._manifest import ImageManifest


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.json"


class TestImageManifest:
    def test_empty_when_missing(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        assert m.all_managed() == {}
        assert m.references_for("foo") == []

    def test_add_reference_creates_file(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        m.add_reference("foo/bar:1", "installer.a")
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data == {"foo/bar:1": ["installer.a"]}

    def test_add_reference_is_idempotent(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        m.add_reference("foo/bar:1", "installer.a")
        m.add_reference("foo/bar:1", "installer.a")
        assert m.references_for("foo/bar:1") == ["installer.a"]

    def test_multiple_installers_per_image(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        m.add_reference("foo/bar:1", "installer.a")
        m.add_reference("foo/bar:1", "installer.b")
        refs = m.references_for("foo/bar:1")
        assert sorted(refs) == ["installer.a", "installer.b"]

    def test_remove_reference(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        m.add_reference("foo:1", "a")
        m.add_reference("foo:1", "b")
        m.remove_reference("foo:1", "a")
        assert m.references_for("foo:1") == ["b"]

    def test_remove_last_reference_removes_image_entry(
        self, manifest_path: Path
    ) -> None:
        m = ImageManifest(manifest_path)
        m.add_reference("foo:1", "a")
        m.remove_reference("foo:1", "a")
        assert m.all_managed() == {}
        assert not m.references_for("foo:1")

    def test_remove_unknown_is_noop(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        m.add_reference("foo:1", "a")
        m.remove_reference("foo:1", "b")  # b never added
        assert m.references_for("foo:1") == ["a"]

    def test_is_managed(self, manifest_path: Path) -> None:
        m = ImageManifest(manifest_path)
        assert not m.is_managed("foo:1")
        m.add_reference("foo:1", "a")
        assert m.is_managed("foo:1")

    def test_corrupted_file_returns_empty(self, manifest_path: Path) -> None:
        manifest_path.write_text("not json at all")
        m = ImageManifest(manifest_path)
        assert m.all_managed() == {}
