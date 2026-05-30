"""Tests for Docker primitives. All subprocess + httpx calls are mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.installers._manifest import ImageManifest
from openjarvis.installers.base import (
    InstallerError,
    Progress,
    StepStatus,
    StorageKind,
    VolumeMount,
    Wipeability,
)
from openjarvis.installers.primitives.docker import (
    DockerEnvCheck,
    DockerImage,
    DockerRun,
    WaitForHTTP,
    _run_docker,
    docker_available,
    docker_cli_present,
    docker_installed,
    list_managed_images,
    start_docker_desktop,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _fake_completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker", "fake"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class _FakePopen:
    """Stand-in for subprocess.Popen used by DockerImage.install()."""

    def __init__(self, lines: List[str], returncode: int = 0) -> None:
        self.stdout = iter([f"{ln}\n" for ln in lines])
        self._returncode = returncode

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def wait(self) -> int:
        return self._returncode


# ---------------------------------------------------------------------------
# _run_docker error translation
# ---------------------------------------------------------------------------


class TestRunDocker:
    def test_missing_docker_raises_installer_error(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("nope")):
            with pytest.raises(InstallerError, match="Docker CLI not found"):
                _run_docker(["version"])

    def test_timeout_raises_installer_error(self) -> None:
        def _boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

        with patch("subprocess.run", side_effect=_boom):
            with pytest.raises(InstallerError, match="timed out"):
                _run_docker(["pull", "foo"], timeout=1)


# ---------------------------------------------------------------------------
# docker_available
# ---------------------------------------------------------------------------


class TestDockerAvailable:
    def test_true_when_version_succeeds(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0, stdout="24.0.0\n"),
        ):
            assert docker_available() is True

    def test_false_when_returncode_nonzero(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=1, stderr="not running"),
        ):
            assert docker_available() is False

    def test_false_when_installer_error(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=InstallerError("nope"),
        ):
            assert docker_available() is False


# ---------------------------------------------------------------------------
# DockerEnvCheck
# ---------------------------------------------------------------------------


class TestDockerEnvCheck:
    def test_detect_installed_when_available(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ):
            assert DockerEnvCheck().detect() == StepStatus.INSTALLED

    def test_detect_partial_when_installed_but_stopped(self) -> None:
        # Daemon down but Docker is installed → PARTIAL, so run() invokes
        # install() (which auto-starts), rather than skipping or erroring.
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker.docker_installed",
            return_value=True,
        ):
            assert DockerEnvCheck().detect() == StepStatus.PARTIAL

    def test_detect_not_installed_when_missing(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker.docker_installed",
            return_value=False,
        ):
            assert DockerEnvCheck().detect() == StepStatus.NOT_INSTALLED

    def test_install_raises_with_link_when_docker_missing(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker.docker_installed",
            return_value=False,
        ):
            with pytest.raises(InstallerError, match="Docker Desktop") as ei:
                list(DockerEnvCheck().install())
            # The error carries the clickable download link.
            assert ei.value.link is not None
            assert ei.value.link["url"] == DockerEnvCheck.INSTALL_URL

    def test_install_yields_done_when_available(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ):
            events = list(DockerEnvCheck().install())
            assert any(e.percent == 100.0 for e in events)

    def test_install_auto_starts_when_stopped(self) -> None:
        # available: False at the top, then True once we poll after start.
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            side_effect=[False, True],
        ), patch(
            "openjarvis.installers.primitives.docker.docker_installed",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker.start_docker_desktop",
            return_value=True,
        ) as mock_start, patch(
            "openjarvis.installers.primitives.docker.time.sleep",
        ):
            events = list(DockerEnvCheck().install())
        mock_start.assert_called_once()
        assert any("Starting it" in e.message for e in events)
        assert events[-1].percent == 100.0

    def test_install_raises_when_start_fails(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker.docker_installed",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker.start_docker_desktop",
            return_value=False,
        ):
            with pytest.raises(InstallerError, match="couldn't be started"):
                list(DockerEnvCheck().install())

    def test_install_times_out_if_engine_never_responds(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker.docker_installed",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker.start_docker_desktop",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._DAEMON_START_TIMEOUT",
            0.0,
        ), patch(
            "openjarvis.installers.primitives.docker.time.sleep",
        ):
            with pytest.raises(InstallerError, match="become responsive"):
                list(DockerEnvCheck().install())


# ---------------------------------------------------------------------------
# Detection + start helpers
# ---------------------------------------------------------------------------


class TestDockerStartHelpers:
    def test_cli_present_true(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0, stdout="27.1.1\n"),
        ):
            assert docker_cli_present() is True

    def test_cli_present_false_when_nonzero(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=1),
        ):
            assert docker_cli_present() is False

    def test_cli_present_false_on_installer_error(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=InstallerError("missing"),
        ):
            assert docker_cli_present() is False

    def test_installed_true_via_cli(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_cli_present",
            return_value=True,
        ):
            assert docker_installed() is True

    def test_installed_true_via_exe(self) -> None:
        # CLI not on PATH, but the Desktop executable is on disk.
        with patch(
            "openjarvis.installers.primitives.docker.docker_cli_present",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker._find_docker_desktop_exe",
            return_value=Path(
                r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
            ),
        ):
            assert docker_installed() is True

    def test_installed_false_when_no_cli_no_exe(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_cli_present",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker._find_docker_desktop_exe",
            return_value=None,
        ):
            assert docker_installed() is False

    def test_start_non_windows_returns_false(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.sys.platform", "linux"
        ):
            assert start_docker_desktop() is False

    def test_start_uses_cli_first(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.sys.platform", "win32"
        ), patch(
            "openjarvis.installers.primitives.docker._docker_desktop_cli_start",
            return_value=True,
        ) as mock_cli, patch(
            "openjarvis.installers.primitives.docker._launch_docker_desktop_exe",
            return_value=False,
        ) as mock_exe:
            assert start_docker_desktop() is True
        mock_cli.assert_called_once()
        mock_exe.assert_not_called()

    def test_start_falls_back_to_exe(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.sys.platform", "win32"
        ), patch(
            "openjarvis.installers.primitives.docker._docker_desktop_cli_start",
            return_value=False,
        ), patch(
            "openjarvis.installers.primitives.docker._launch_docker_desktop_exe",
            return_value=True,
        ) as mock_exe:
            assert start_docker_desktop() is True
        mock_exe.assert_called_once()


# ---------------------------------------------------------------------------
# DockerImage
# ---------------------------------------------------------------------------


class TestDockerImage:
    def test_ref_normalization_appends_latest(self) -> None:
        assert DockerImage("foo/bar").image_ref == "foo/bar:latest"

    def test_ref_normalization_preserves_existing_tag(self) -> None:
        assert DockerImage("foo/bar:v1").image_ref == "foo/bar:v1"

    def test_ref_normalization_handles_registry_port(self) -> None:
        # ghcr.io:443/foo/bar should not be confused into thinking
        # ":443" is a tag.
        assert (
            DockerImage("ghcr.io:443/foo/bar").image_ref
            == "ghcr.io:443/foo/bar:latest"
        )

    def test_detect_installed(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0),
        ):
            assert DockerImage("foo:1").detect() == StepStatus.INSTALLED

    def test_detect_not_installed(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=1),
        ):
            assert DockerImage("foo:1").detect() == StepStatus.NOT_INSTALLED

    def test_install_skips_when_already_present(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.json"
        img = DockerImage("foo:1")
        img._bind("test.installer")
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0),
        ), patch(
            "openjarvis.installers.primitives.docker.ImageManifest",
            return_value=ImageManifest(manifest_path),
        ):
            events = list(img.install())
            assert events
            assert events[-1].percent == 100.0
            # Manifest should still record the reference.
            assert "test.installer" in ImageManifest(manifest_path).references_for("foo:1")

    def test_install_streams_pull_progress(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.json"
        img = DockerImage("foo:1")
        img._bind("test.installer")

        # Simulate detect = not installed, so we fall into the pull path.
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=1),
        ), patch(
            "openjarvis.installers.primitives.docker.subprocess.Popen",
            return_value=_FakePopen(
                lines=[
                    "abc: Pulling fs layer",
                    "abc: Pull complete",
                    "def: Pull complete",
                    "ghi: Pull complete",
                    "Status: Downloaded newer image for foo:1",
                ],
                returncode=0,
            ),
        ), patch(
            "openjarvis.installers.primitives.docker.ImageManifest",
            return_value=ImageManifest(manifest_path),
        ):
            events = list(img.install())

        # First event: 0% pulling.
        assert events[0].percent == 0.0
        # Last event: 100% pulled.
        assert events[-1].percent == 100.0
        # Manifest recorded.
        assert "test.installer" in ImageManifest(manifest_path).references_for("foo:1")

    def test_install_raises_on_pull_failure(self, tmp_path: Path) -> None:
        img = DockerImage("foo:1")
        img._bind("test.installer")
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=1),
        ), patch(
            "openjarvis.installers.primitives.docker.subprocess.Popen",
            return_value=_FakePopen(lines=["error"], returncode=1),
        ), patch(
            "openjarvis.installers.primitives.docker.ImageManifest",
            return_value=ImageManifest(tmp_path / "m.json"),
        ):
            with pytest.raises(InstallerError, match="docker pull"):
                list(img.install())

    def test_uninstall_drops_manifest_only(self, tmp_path: Path) -> None:
        """Uninstall must NOT actually remove the image — global resource."""
        manifest_path = tmp_path / "m.json"
        mgr = ImageManifest(manifest_path)
        mgr.add_reference("foo:1", "test.installer")

        with patch(
            "openjarvis.installers.primitives.docker.ImageManifest",
            return_value=mgr,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=AssertionError("uninstall should NOT call docker"),
        ):
            img = DockerImage("foo:1")
            img._bind("test.installer")
            img.uninstall()
            # Manifest reference dropped, no docker rmi call.
            assert mgr.references_for("foo:1") == []

    def test_storage_inventory_is_empty(self) -> None:
        """Images are global resources — not per-installer storage."""
        assert DockerImage("foo:1").storage_inventory() == []


# ---------------------------------------------------------------------------
# DockerRun
# ---------------------------------------------------------------------------


class TestDockerRun:
    def _vol(self, host: Path, *, wipeability: Wipeability) -> VolumeMount:
        return VolumeMount(
            host_path=host,
            container_path="/etc/foo",
            item_id="foo.data",
            description="data dir",
            wipeability=wipeability,
        )

    def test_detect_returns_not_installed_when_no_container(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=1),  # inspect fails
        ):
            run = DockerRun(container_name="c", image_ref="img:1")
            assert run.detect() == StepStatus.NOT_INSTALLED

    def test_detect_running_is_installed(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0, stdout="running\n"),
        ):
            run = DockerRun(container_name="c", image_ref="img:1")
            assert run.detect() == StepStatus.INSTALLED

    def test_detect_stopped_is_partial(self) -> None:
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0, stdout="exited\n"),
        ):
            run = DockerRun(container_name="c", image_ref="img:1")
            assert run.detect() == StepStatus.PARTIAL

    def test_install_creates_container_with_args(self, tmp_path: Path) -> None:
        host_dir = tmp_path / "data"
        run = DockerRun(
            container_name="c",
            image_ref="img:1",
            ports={8888: 8080},
            volumes=[self._vol(host_dir, wipeability=Wipeability.EPHEMERAL)],
            env={"FOO": "bar"},
        )

        call_args: List[List[str]] = []

        def _fake(args, **kw):
            call_args.append(list(args))
            # First call: inspect — return "container doesn't exist".
            if len(call_args) == 1 and args[0] == "inspect":
                return _fake_completed(returncode=1)
            # docker run succeeds.
            return _fake_completed(returncode=0, stdout="abc123\n")

        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=_fake,
        ):
            events = list(run.install())

        # Last event is 100%.
        assert events[-1].percent == 100.0
        # Verify docker run was called with the expected args.
        run_args = [a for a in call_args if a and a[0] == "run"][0]
        assert "--name" in run_args
        assert "c" in run_args
        assert "--restart" in run_args
        assert "unless-stopped" in run_args
        assert any(arg.endswith(":/etc/foo") for arg in run_args), (
            f"Expected volume bind in args, got: {run_args}"
        )
        assert "-p" in run_args and "8888:8080" in run_args
        assert "-e" in run_args and "FOO=bar" in run_args

    def test_install_starts_existing_stopped_container(self) -> None:
        call_args: List[List[str]] = []

        def _fake(args, **kw):
            call_args.append(list(args))
            if args[0] == "inspect":
                return _fake_completed(returncode=0, stdout="exited\n")
            if args[0] == "start":
                return _fake_completed(returncode=0)
            raise AssertionError(f"unexpected docker call: {args}")

        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=_fake,
        ):
            run = DockerRun(container_name="c", image_ref="img:1")
            list(run.install())

        commands = [a[0] for a in call_args]
        assert "start" in commands
        assert "run" not in commands  # didn't create a new container

    def test_install_skips_when_already_running(self) -> None:
        call_args: List[List[str]] = []

        def _fake(args, **kw):
            call_args.append(list(args))
            return _fake_completed(returncode=0, stdout="running\n")

        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=_fake,
        ):
            run = DockerRun(container_name="c", image_ref="img:1")
            events = list(run.install())

        assert events[-1].percent == 100.0
        assert all(a[0] != "start" and a[0] != "run" for a in call_args)

    def test_storage_inventory_reports_volumes(self, tmp_path: Path) -> None:
        host = tmp_path / "data"
        host.mkdir()
        (host / "blob").write_text("xxxxxxxx")  # 8 bytes
        run = DockerRun(
            container_name="c",
            image_ref="img:1",
            volumes=[self._vol(host, wipeability=Wipeability.EPHEMERAL)],
        )
        items = run.storage_inventory()
        assert len(items) == 1
        assert items[0].kind == StorageKind.VOLUME
        assert items[0].size_bytes == 8
        assert items[0].wipeability == Wipeability.EPHEMERAL

    def test_storage_inventory_respects_report_storage_false(
        self, tmp_path: Path
    ) -> None:
        host = tmp_path / "data.txt"
        host.write_text("data")
        run = DockerRun(
            container_name="c",
            image_ref="img:1",
            volumes=[
                VolumeMount(
                    host_path=host,
                    container_path="/etc/foo",
                    item_id="foo.config",
                    description="config",
                    wipeability=Wipeability.REPLACEABLE,
                    report_storage=False,
                )
            ],
        )
        assert run.storage_inventory() == []

    def test_needs_restart_for_wipe(self, tmp_path: Path) -> None:
        host = tmp_path / "d"
        host.mkdir()
        run = DockerRun(
            container_name="c",
            image_ref="img:1",
            volumes=[self._vol(host, wipeability=Wipeability.EPHEMERAL)],
        )
        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            return_value=_fake_completed(returncode=0, stdout="running\n"),
        ):
            assert run.needs_restart_for_wipe({"foo.data"}) is True
            assert run.needs_restart_for_wipe({"other.id"}) is False

    def test_stop_and_start_call_docker(self) -> None:
        commands: List[str] = []

        def _fake(args, **kw):
            commands.append(args[0])
            return _fake_completed(returncode=0)

        with patch(
            "openjarvis.installers.primitives.docker.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=_fake,
        ):
            run = DockerRun(container_name="c", image_ref="img:1")
            run.stop()
            run.start()

        assert commands == ["stop", "start"]


# ---------------------------------------------------------------------------
# WaitForHTTP
# ---------------------------------------------------------------------------


class TestWaitForHTTP:
    def test_detect_returns_installed_when_ok(self) -> None:
        with patch.object(
            WaitForHTTP, "_check", return_value=True
        ):
            assert WaitForHTTP("http://x").detect() == StepStatus.INSTALLED

    def test_install_returns_immediately_when_ready(self) -> None:
        with patch.object(WaitForHTTP, "_check", return_value=True):
            events = list(WaitForHTTP("http://x").install())
            assert events[-1].percent == 100.0

    def test_install_retries_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def _flake(self):
            attempts["n"] += 1
            return attempts["n"] >= 3

        with patch.object(WaitForHTTP, "_check", _flake), patch(
            "openjarvis.installers.primitives.docker.time.sleep"
        ):
            events = list(
                WaitForHTTP("http://x", timeout=10, interval=0.01).install()
            )
        assert events[-1].percent == 100.0
        assert attempts["n"] >= 3

    def test_install_raises_on_timeout(self) -> None:
        with patch.object(
            WaitForHTTP, "_check", return_value=False
        ), patch("openjarvis.installers.primitives.docker.time.sleep"):
            with pytest.raises(InstallerError, match="Timed out"):
                list(
                    WaitForHTTP(
                        "http://x", timeout=0.5, interval=0.1
                    ).install()
                )


# ---------------------------------------------------------------------------
# list_managed_images
# ---------------------------------------------------------------------------


class TestListManagedImages:
    def test_aggregates_from_manifest(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.json"
        mgr = ImageManifest(manifest_path)
        mgr.add_reference("foo:1", "installer.a")
        mgr.add_reference("bar:2", "installer.b")

        def _fake(args, **kw):
            if args[0:2] == ["image", "inspect"]:
                # Return a stable fake size keyed on image name.
                if "foo" in args[2]:
                    return _fake_completed(returncode=0, stdout="1024\n")
                return _fake_completed(returncode=0, stdout="2048\n")
            return _fake_completed(returncode=0)

        with patch(
            "openjarvis.installers.primitives.docker.ImageManifest",
            return_value=mgr,
        ), patch(
            "openjarvis.installers.primitives.docker._run_docker",
            side_effect=_fake,
        ):
            out = list_managed_images()

        by_ref = {row["image_ref"]: row for row in out}
        assert by_ref["foo:1"]["size_bytes"] == 1024
        assert by_ref["foo:1"]["installer_ids"] == ["installer.a"]
        assert by_ref["foo:1"]["image_exists"] is True
        assert by_ref["bar:2"]["size_bytes"] == 2048
