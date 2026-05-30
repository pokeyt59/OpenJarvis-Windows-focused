"""Tests for the installer base protocols + Installer orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

from openjarvis.installers.base import (
    BaseStep,
    Installer,
    InstallerError,
    OverallStatus,
    Progress,
    ProgressLevel,
    StepStatus,
    StorageItem,
    StorageKind,
    VolumeMount,
    WipeRefused,
    Wipeability,
    measure_path_bytes,
)


# ---------------------------------------------------------------------------
# Test helpers — minimal step implementations
# ---------------------------------------------------------------------------


class _ScriptedStep(BaseStep):
    """A step that just plays back the events you give it.

    Knobs to simulate every interesting condition: pre-existing INSTALLED
    state, install() raising, verify() returning False, owned storage,
    needs_restart_for_wipe behavior.
    """

    def __init__(
        self,
        *,
        name: str,
        initial: StepStatus = StepStatus.NOT_INSTALLED,
        raise_in_install: bool = False,
        verify_returns: bool = True,
        items: List[StorageItem] | None = None,
        needs_restart: bool = False,
    ) -> None:
        self.name = name
        self.description = f"scripted: {name}"
        self._state = initial
        self._raise = raise_in_install
        self._verify = verify_returns
        self._items = items or []
        self._needs_restart = needs_restart
        self.stop_calls = 0
        self.start_calls = 0
        self.install_calls = 0
        self.uninstall_calls = 0
        self.bound_installer_id: str | None = None

    # Binding hook ---------------------------------------------------------

    def _bind(self, installer_id: str) -> None:
        self.bound_installer_id = installer_id

    # Lifecycle ------------------------------------------------------------

    def detect(self) -> StepStatus:
        return self._state

    def install(self) -> Iterator[Progress]:
        self.install_calls += 1
        if self._raise:
            raise InstallerError(f"{self.name} blew up")
        yield Progress(step_idx=0, step_name=self.name, percent=50.0, message="halfway")
        self._state = StepStatus.INSTALLED
        yield Progress(step_idx=0, step_name=self.name, percent=100.0, message="done")

    def verify(self) -> bool:
        return self._verify

    def uninstall(self) -> None:
        self.uninstall_calls += 1
        self._state = StepStatus.NOT_INSTALLED

    def storage_inventory(self) -> List[StorageItem]:
        return list(self._items)

    def needs_restart_for_wipe(self, item_ids: set) -> bool:
        my_ids = {i.item_id for i in self._items}
        return self._needs_restart and bool(my_ids & set(item_ids))

    def stop(self) -> None:
        self.stop_calls += 1

    def start(self) -> None:
        self.start_calls += 1


# ---------------------------------------------------------------------------
# Installer.status() / run()
# ---------------------------------------------------------------------------


class TestInstallerStatus:
    def test_empty_installer_is_ready(self) -> None:
        inst = Installer(installer_id="empty", display_name="Empty")
        assert inst.status() == OverallStatus.READY

    def test_all_not_installed(self) -> None:
        inst = Installer(
            installer_id="x",
            display_name="X",
            steps=[
                _ScriptedStep(name="a", initial=StepStatus.NOT_INSTALLED),
                _ScriptedStep(name="b", initial=StepStatus.NOT_INSTALLED),
            ],
        )
        assert inst.status() == OverallStatus.NOT_INSTALLED

    def test_all_installed_is_ready(self) -> None:
        inst = Installer(
            installer_id="x",
            display_name="X",
            steps=[
                _ScriptedStep(name="a", initial=StepStatus.INSTALLED),
                _ScriptedStep(name="b", initial=StepStatus.INSTALLED),
            ],
        )
        assert inst.status() == OverallStatus.READY

    def test_partial(self) -> None:
        inst = Installer(
            installer_id="x",
            display_name="X",
            steps=[
                _ScriptedStep(name="a", initial=StepStatus.INSTALLED),
                _ScriptedStep(name="b", initial=StepStatus.NOT_INSTALLED),
            ],
        )
        assert inst.status() == OverallStatus.PARTIAL

    def test_broken_dominates(self) -> None:
        inst = Installer(
            installer_id="x",
            display_name="X",
            steps=[
                _ScriptedStep(name="a", initial=StepStatus.INSTALLED),
                _ScriptedStep(name="b", initial=StepStatus.BROKEN),
            ],
        )
        assert inst.status() == OverallStatus.BROKEN


class TestInstallerRun:
    def test_runs_each_step(self) -> None:
        a = _ScriptedStep(name="a", initial=StepStatus.NOT_INSTALLED)
        b = _ScriptedStep(name="b", initial=StepStatus.NOT_INSTALLED)
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])
        events = list(inst.run())
        assert a.install_calls == 1
        assert b.install_calls == 1
        # Should see at least one event per step.
        names = {e.step_name for e in events}
        assert names == {"a", "b"}

    def test_skips_already_installed(self) -> None:
        a = _ScriptedStep(name="a", initial=StepStatus.INSTALLED)
        b = _ScriptedStep(name="b", initial=StepStatus.NOT_INSTALLED)
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])
        list(inst.run())
        assert a.install_calls == 0
        assert b.install_calls == 1

    def test_re_emits_with_correct_step_idx(self) -> None:
        a = _ScriptedStep(name="a", initial=StepStatus.NOT_INSTALLED)
        b = _ScriptedStep(name="b", initial=StepStatus.NOT_INSTALLED)
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])
        events = list(inst.run())
        # Step a events should have idx=0, step b events idx=1
        a_idxs = {e.step_idx for e in events if e.step_name == "a"}
        b_idxs = {e.step_idx for e in events if e.step_name == "b"}
        assert a_idxs == {0}
        assert b_idxs == {1}

    def test_install_error_stops_run(self) -> None:
        a = _ScriptedStep(name="a", initial=StepStatus.NOT_INSTALLED, raise_in_install=True)
        b = _ScriptedStep(name="b", initial=StepStatus.NOT_INSTALLED)
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])

        with pytest.raises(InstallerError):
            list(inst.run())
        # b should not have been touched
        assert b.install_calls == 0

    def test_verify_failure_raises(self) -> None:
        a = _ScriptedStep(
            name="a",
            initial=StepStatus.NOT_INSTALLED,
            verify_returns=False,
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        with pytest.raises(InstallerError):
            list(inst.run())


# ---------------------------------------------------------------------------
# Bind hook
# ---------------------------------------------------------------------------


class TestBindHook:
    def test_post_init_binds_steps(self) -> None:
        a = _ScriptedStep(name="a")
        b = _ScriptedStep(name="b")
        Installer(installer_id="my.installer", display_name="X", steps=[a, b])
        assert a.bound_installer_id == "my.installer"
        assert b.bound_installer_id == "my.installer"

    def test_bind_failure_doesnt_break_init(self) -> None:
        class _BoomStep(BaseStep):
            name = "boom"

            def _bind(self, installer_id: str) -> None:
                raise RuntimeError("nope")

        # Should not raise — bind failures are logged.
        Installer(installer_id="x", display_name="X", steps=[_BoomStep()])


# ---------------------------------------------------------------------------
# Storage aggregation
# ---------------------------------------------------------------------------


class TestStorageInventory:
    def test_empty(self) -> None:
        inst = Installer(installer_id="x", display_name="X")
        report = inst.storage_inventory()
        assert report.items == []
        assert report.total_bytes == 0

    def test_aggregates_across_steps(self) -> None:
        a = _ScriptedStep(
            name="a",
            items=[
                StorageItem(
                    item_id="cache",
                    kind=StorageKind.VOLUME,
                    description="cache",
                    size_bytes=500,
                    wipeability=Wipeability.EPHEMERAL,
                )
            ],
        )
        b = _ScriptedStep(
            name="b",
            items=[
                StorageItem(
                    item_id="config",
                    kind=StorageKind.CONFIG,
                    description="config",
                    size_bytes=100,
                    wipeability=Wipeability.REPLACEABLE,
                )
            ],
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])
        report = inst.storage_inventory()
        assert {i.item_id for i in report.items} == {"cache", "config"}
        assert report.total_bytes == 600
        assert report.by_kind[StorageKind.VOLUME] == 500
        assert report.by_kind[StorageKind.CONFIG] == 100

    def test_dedupes_by_item_id(self) -> None:
        """When two steps report the same item_id, only the first wins."""
        item = StorageItem(
            item_id="shared",
            kind=StorageKind.CONFIG,
            description="shared file",
            size_bytes=42,
            wipeability=Wipeability.REPLACEABLE,
        )
        a = _ScriptedStep(name="a", items=[item])
        b = _ScriptedStep(
            name="b",
            items=[
                StorageItem(
                    item_id="shared",  # duplicate id
                    kind=StorageKind.VOLUME,  # different kind, ignored
                    description="b's view",
                    size_bytes=99999,  # would double-count if not deduped
                    wipeability=Wipeability.IRRECOVERABLE,
                )
            ],
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])
        report = inst.storage_inventory()
        assert len(report.items) == 1
        assert report.items[0].description == "shared file"  # a wins
        assert report.total_bytes == 42


# ---------------------------------------------------------------------------
# Wipe lifecycle
# ---------------------------------------------------------------------------


class TestWipe:
    def _make_step_with_item(
        self,
        item_id: str,
        wipeability: Wipeability,
        *,
        needs_restart: bool = False,
        tmp_path: Path,
    ) -> _ScriptedStep:
        f = tmp_path / f"{item_id}.txt"
        f.write_text("hello")
        return _ScriptedStep(
            name=item_id,
            items=[
                StorageItem(
                    item_id=item_id,
                    kind=StorageKind.CONFIG,
                    description=f"item {item_id}",
                    size_bytes=5,
                    wipeability=wipeability,
                    path=f,
                )
            ],
            needs_restart=needs_restart,
        )

    def test_wipe_deletes_path(self, tmp_path: Path) -> None:
        a = self._make_step_with_item("a", Wipeability.EPHEMERAL, tmp_path=tmp_path)
        path = a._items[0].path
        assert path is not None and path.exists()
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        list(inst.wipe(["a"]))
        assert not path.exists()

    def test_wipe_refuses_irrecoverable_without_force(
        self, tmp_path: Path
    ) -> None:
        a = self._make_step_with_item(
            "a", Wipeability.IRRECOVERABLE, tmp_path=tmp_path
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        with pytest.raises(WipeRefused):
            list(inst.wipe(["a"]))
        path = a._items[0].path
        assert path is not None and path.exists(), "file must survive refused wipe"

    def test_wipe_allows_irrecoverable_with_force(self, tmp_path: Path) -> None:
        a = self._make_step_with_item(
            "a", Wipeability.IRRECOVERABLE, tmp_path=tmp_path
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        list(inst.wipe(["a"], force=True))
        path = a._items[0].path
        assert path is not None and not path.exists()

    def test_wipe_unknown_id_raises(self, tmp_path: Path) -> None:
        a = self._make_step_with_item("a", Wipeability.EPHEMERAL, tmp_path=tmp_path)
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        with pytest.raises(WipeRefused):
            list(inst.wipe(["does-not-exist"]))

    def test_wipe_stops_and_restarts_steps_needing_restart(
        self, tmp_path: Path
    ) -> None:
        a = self._make_step_with_item(
            "a", Wipeability.EPHEMERAL, needs_restart=True, tmp_path=tmp_path
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        list(inst.wipe(["a"]))
        assert a.stop_calls == 1
        assert a.start_calls == 1

    def test_wipe_no_restart_when_restart_after_false(
        self, tmp_path: Path
    ) -> None:
        a = self._make_step_with_item(
            "a", Wipeability.EPHEMERAL, needs_restart=True, tmp_path=tmp_path
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a])
        list(inst.wipe(["a"], restart_after=False))
        assert a.stop_calls == 1
        assert a.start_calls == 0

    def test_wipe_doesnt_touch_unrelated_steps(self, tmp_path: Path) -> None:
        a = self._make_step_with_item(
            "a", Wipeability.EPHEMERAL, needs_restart=True, tmp_path=tmp_path
        )
        b = self._make_step_with_item(
            "b", Wipeability.EPHEMERAL, needs_restart=True, tmp_path=tmp_path
        )
        inst = Installer(installer_id="x", display_name="X", steps=[a, b])
        list(inst.wipe(["a"]))
        assert a.stop_calls == 1
        assert b.stop_calls == 0  # b doesn't own the wiped item


# ---------------------------------------------------------------------------
# measure_path_bytes helper
# ---------------------------------------------------------------------------


class TestMeasurePathBytes:
    def test_file(self, tmp_path: Path) -> None:
        f = tmp_path / "x.txt"
        f.write_text("hello world")
        assert measure_path_bytes(f) == 11

    def test_directory_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("12345")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("123")
        assert measure_path_bytes(tmp_path) == 8

    def test_nonexistent_returns_zero(self, tmp_path: Path) -> None:
        assert measure_path_bytes(tmp_path / "nope") == 0

    def test_none_returns_zero(self) -> None:
        assert measure_path_bytes(None) == 0
