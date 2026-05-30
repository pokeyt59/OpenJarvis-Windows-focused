"""Docker primitives: DockerEnvCheck, DockerImage, DockerRun, WaitForHTTP.

All steps invoke the ``docker`` CLI via subprocess. We never use the
Docker SDK so the only runtime dependency is a working Docker
installation on the host.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

from openjarvis.installers._manifest import ImageManifest
from openjarvis.installers.base import (
    BaseStep,
    InstallerError,
    Progress,
    ProgressLevel,
    StepStatus,
    StorageItem,
    StorageKind,
    VolumeMount,
    measure_path_bytes,
)

logger = logging.getLogger(__name__)


# How long to wait for the Docker engine to answer after we kick off a
# start, and how often to poll while waiting. WSL2 cold starts are
# genuinely slow (the VM + engine can take the better part of a minute),
# so the budget is deliberately generous.
_DAEMON_START_TIMEOUT = 120.0
_DAEMON_POLL_INTERVAL = 3.0

# Timeout for the ``docker desktop start`` CLI itself (Docker Desktop
# 4.37+). That command blocks until the engine is up; cap it a little
# below the overall poll budget so a hung CLI still leaves us room to
# fall back to launching the executable.
_CLI_START_TIMEOUT = 90


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _run_docker(
    args: Sequence[str],
    *,
    timeout: int = 60,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run ``docker <args>`` and return the CompletedProcess.

    Raises :class:`InstallerError` if the docker CLI is missing entirely.
    If ``check`` is True, also raises on non-zero exit. Otherwise the
    caller is responsible for inspecting ``returncode``.
    """
    cmd = ["docker", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except FileNotFoundError as exc:
        raise InstallerError(
            "Docker CLI not found. Install Docker Desktop (Windows/macOS) "
            "or the docker engine (Linux) and retry."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise InstallerError(
            f"docker {' '.join(args)} failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallerError(
            f"docker {' '.join(args)} timed out after {timeout}s"
        ) from exc


def docker_available() -> bool:
    """True if the docker CLI is installed and the daemon is responsive.

    Queries the *server* version, so this is False whenever the daemon
    isn't running — including the common "Docker Desktop installed but
    not launched" case. Use :func:`docker_installed` to tell that case
    apart from "Docker not installed at all".
    """
    try:
        r = _run_docker(
            ["version", "--format", "{{.Server.Version}}"],
            timeout=10,
            check=False,
        )
        return r.returncode == 0
    except InstallerError:
        return False


def docker_cli_present() -> bool:
    """True if the docker CLI is installed, regardless of daemon state.

    Probes the *client* version (``{{.Client.Version}}``), which resolves
    without a running daemon. This is what lets us distinguish "installed
    but stopped" from "not installed at all" — :func:`docker_available`
    can't, because it queries the server version.
    """
    try:
        r = _run_docker(
            ["version", "--format", "{{.Client.Version}}"],
            timeout=10,
            check=False,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except InstallerError:
        return False


def _find_docker_desktop_exe() -> Optional[Path]:
    """Locate the Docker Desktop executable on Windows, else ``None``.

    Checks the standard install locations under the various Program Files
    environment variables, plus a hard-coded default in case those aren't
    set. Non-Windows platforms always return ``None`` — we don't manage
    the daemon there.
    """
    if sys.platform != "win32":
        return None
    seen: set = set()
    candidates: List[Path] = []
    for env_var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(
                Path(base) / "Docker" / "Docker" / "Docker Desktop.exe"
            )
    # Hard-coded fallback for the (rare) case the env vars are missing.
    candidates.append(
        Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
    )
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def docker_installed() -> bool:
    """True if Docker appears installed even when the daemon is stopped.

    Returns True when either the CLI resolves (:func:`docker_cli_present`)
    or, on Windows, the Docker Desktop executable is found on disk. The
    latter covers a fresh install whose CLI isn't on PATH yet (no shell
    restart) — we can still recognize it as "installed, just not running".
    """
    if docker_cli_present():
        return True
    return _find_docker_desktop_exe() is not None


def _docker_desktop_cli_start() -> bool:
    """Try the official ``docker desktop start`` subcommand.

    Available on Docker Desktop 4.37+ (late 2024). Returns True only when
    the subcommand exists and exits 0; older builds lack it and exit
    non-zero, so the caller can fall back to launching the executable. A
    timeout is treated as "didn't work" for the same reason.
    """
    try:
        r = _run_docker(
            ["desktop", "start"],
            timeout=_CLI_START_TIMEOUT,
            check=False,
        )
        return r.returncode == 0
    except InstallerError:
        return False


def _launch_docker_desktop_exe() -> bool:
    """Spawn the Docker Desktop app on Windows. Returns True if launched.

    Docker Desktop drops to the system tray. There's no documented flag
    to suppress its first-ever onboarding window, but subsequent launches
    are tray-only. We spawn detached so the engine keeps starting while
    this process moves on to polling for the daemon.
    """
    exe = _find_docker_desktop_exe()
    if exe is None:
        return False
    try:
        subprocess.Popen(
            [str(exe)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # No-op for a GUI app, but keeps a console window from
            # flashing if the parent backend ever owns one. Guarded with
            # getattr so the attribute access is safe off-Windows.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        return True
    except OSError as exc:
        logger.warning("Failed to launch Docker Desktop: %s", exc)
        return False


def start_docker_desktop() -> bool:
    """Best-effort start of the Docker engine on Windows (hybrid strategy).

    1. ``docker desktop start`` — the official CLI (Docker Desktop 4.37+).
       Clean and silent; drops to the tray and returns once the engine is
       starting/ready.
    2. Launch ``Docker Desktop.exe`` directly — works on every Desktop
       version; also drops to the tray.

    Returns True if *some* start path was initiated. The caller is still
    responsible for polling :func:`docker_available` until the daemon
    answers. Non-Windows platforms return False — we don't manage their
    daemons (Linux uses systemd, which needs privileges we won't assume).
    """
    if sys.platform != "win32":
        return False
    if _docker_desktop_cli_start():
        return True
    return _launch_docker_desktop_exe()


def list_managed_images() -> List[Dict[str, Any]]:
    """Return rich info about every image OpenJarvis has pulled.

    For each image in the manifest, looks up current disk size via
    ``docker image inspect`` and reports which installers (if any) still
    reference it. Used by the global Docker resources page.

    Returns a list of dicts::

        [{
            "image_ref": "searxng/searxng:latest",
            "size_bytes": 358916096,
            "installer_ids": ["web_search.searxng"],
            "in_use": True,        # at least one installer still references it
            "image_exists": True,  # actually present on disk
        }]
    """
    manifest = ImageManifest().all_managed()
    out: List[Dict[str, Any]] = []
    for image_ref, installer_ids in manifest.items():
        size = 0
        exists = False
        try:
            r = _run_docker(
                ["image", "inspect", image_ref, "--format", "{{.Size}}"],
                timeout=10,
                check=False,
            )
            if r.returncode == 0 and r.stdout.strip().isdigit():
                size = int(r.stdout.strip())
                exists = True
        except InstallerError:
            pass
        out.append(
            {
                "image_ref": image_ref,
                "size_bytes": size,
                "installer_ids": list(installer_ids),
                "in_use": bool(installer_ids),
                "image_exists": exists,
            }
        )
    return out


# ---------------------------------------------------------------------------
# DockerEnvCheck
# ---------------------------------------------------------------------------


class DockerEnvCheck(BaseStep):
    """Verifies Docker is installed and the daemon is responsive.

    Detection is three-state:

      * **INSTALLED**  — the daemon answers; nothing to do.
      * **PARTIAL**    — Docker Desktop is installed but the daemon isn't
        running. ``install()`` will try to start it automatically and
        wait for the engine.
      * **NOT_INSTALLED** — no Docker at all; we point the user at the
        download page (we don't auto-install Docker itself: it needs
        elevated rights and has platform-specific quirks).

    The PARTIAL case is the important one — returning it (rather than
    NOT_INSTALLED) is what makes :meth:`Installer.run` invoke
    :meth:`install`, which performs the auto-start.
    """

    INSTALL_URL = "https://www.docker.com/products/docker-desktop/"

    def __init__(self) -> None:
        self.name = "docker-env-check"
        self.description = "Verify Docker is installed and responsive"

    def detect(self) -> StepStatus:
        if docker_available():
            return StepStatus.INSTALLED
        if docker_installed():
            # Installed but the daemon isn't responding yet. PARTIAL (not
            # NOT_INSTALLED) so the installer's run() invokes install(),
            # which auto-starts Docker Desktop and waits for the engine.
            return StepStatus.PARTIAL
        return StepStatus.NOT_INSTALLED

    def install(self) -> Iterator[Progress]:
        if docker_available():
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=100.0,
                message="Docker is installed and the daemon is responsive.",
            )
            return

        if not docker_installed():
            raise InstallerError(
                # The frontend renders ``link`` as a clickable button next
                # to the message, so we don't splice the URL into the text.
                # Keeps the sentence clean and gives the user a single
                # click to fix the problem.
                "Docker is not available on this machine. "
                "Install Docker Desktop and try again.",
                link={
                    "label": "Install Docker Desktop",
                    "url": self.INSTALL_URL,
                },
            )

        # Installed but the daemon isn't responding — try to start Docker
        # Desktop ourselves so the user doesn't have to. This also runs as
        # the first step of dependent installers (e.g. SearXNG), so those
        # installs auto-start Docker for free.
        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=5.0,
            message="Docker Desktop is installed but not running. Starting it...",
        )

        if not start_docker_desktop():
            # Couldn't even initiate a start (non-Windows, or the
            # executable wasn't found). Fall back to a manual instruction;
            # no link, since the download page is unhelpful when it's
            # already installed.
            raise InstallerError(
                "Docker Desktop is installed but couldn't be started "
                "automatically. Start Docker Desktop manually, wait for "
                "the whale icon in the system tray, and try again."
            )

        # Poll until the engine answers (WSL2 cold starts are slow).
        start_t = time.monotonic()
        while True:
            if docker_available():
                yield Progress(
                    step_idx=0,
                    step_name=self.name,
                    percent=100.0,
                    message="Docker Desktop started — the daemon is responsive.",
                )
                return

            elapsed = time.monotonic() - start_t
            if elapsed >= _DAEMON_START_TIMEOUT:
                raise InstallerError(
                    "Docker Desktop was started but its engine didn't "
                    f"become responsive within {int(_DAEMON_START_TIMEOUT)}s. "
                    "It may still be starting up — wait a moment and try "
                    "again."
                )

            pct = min(95.0, 5.0 + (elapsed / _DAEMON_START_TIMEOUT) * 90.0)
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=pct,
                message=f"Waiting for the Docker engine to start ({int(elapsed)}s)...",
            )
            time.sleep(_DAEMON_POLL_INTERVAL)

    def verify(self) -> bool:
        return docker_available()


# ---------------------------------------------------------------------------
# DockerImage
# ---------------------------------------------------------------------------


class DockerImage(BaseStep):
    """Ensures a Docker image is present, pulling if necessary.

    Each install records the image in the global manifest under the
    owning ``installer_id`` so the Docker resources page can attribute
    usage. ``uninstall()`` removes the manifest reference but does NOT
    ``docker rmi`` — images are global resources managed centrally.
    """

    def __init__(self, image_ref: str) -> None:
        self.image_ref = self._normalize_ref(image_ref)
        self._installer_id: Optional[str] = None
        self.name = f"docker-image:{self.image_ref}"
        self.description = f"Pull Docker image {self.image_ref}"

    # Installer wiring -----------------------------------------------------

    def _bind(self, installer_id: str) -> None:
        """Called by :class:`Installer` so we can record manifest refs."""
        self._installer_id = installer_id

    # Helpers --------------------------------------------------------------

    @staticmethod
    def _normalize_ref(ref: str) -> str:
        """Append ``:latest`` if no tag is present."""
        # The tag is the part after the last colon, but a port in the
        # registry hostname can also contain a colon ("ghcr.io:443/foo").
        # The robust check: tag if the segment after the final '/' has a colon.
        last_segment = ref.rsplit("/", 1)[-1]
        return ref if ":" in last_segment else f"{ref}:latest"

    def _record_manifest(self) -> None:
        if self._installer_id:
            try:
                ImageManifest().add_reference(self.image_ref, self._installer_id)
            except Exception as exc:
                logger.warning("Failed to record image manifest entry: %s", exc)

    # Lifecycle ------------------------------------------------------------

    def detect(self) -> StepStatus:
        if not docker_available():
            return StepStatus.UNKNOWN
        try:
            r = _run_docker(
                ["image", "inspect", self.image_ref],
                timeout=15,
                check=False,
            )
            return (
                StepStatus.INSTALLED
                if r.returncode == 0
                else StepStatus.NOT_INSTALLED
            )
        except InstallerError:
            return StepStatus.UNKNOWN

    def install(self) -> Iterator[Progress]:
        if self.detect() == StepStatus.INSTALLED:
            self._record_manifest()
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=100.0,
                message=f"Image {self.image_ref} already present.",
            )
            return

        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=0.0,
            message=f"Pulling {self.image_ref} (this may take a few minutes)...",
        )

        # Stream `docker pull` output line-by-line so we can emit coarse
        # progress events ("Pulled N layers"). Full progress bars would
        # require parsing the per-layer "Downloading [===> ] 50/100 MB"
        # status lines; coarse layer-completion counts are good enough
        # for v1 and don't depend on Docker's status line format.
        last_emitted_pct = 0.0
        layer_completes = 0
        try:
            proc = subprocess.Popen(
                ["docker", "pull", self.image_ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise InstallerError("Docker CLI disappeared mid-install.") from exc

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            logger.debug("docker pull: %s", line)
            if "Pull complete" in line:
                layer_completes += 1
                # Cap at 90% — final 10% covers post-pull manifest record.
                pct = min(90.0, 10.0 + layer_completes * 7.0)
                if pct >= last_emitted_pct + 5.0:
                    last_emitted_pct = pct
                    yield Progress(
                        step_idx=0,
                        step_name=self.name,
                        percent=pct,
                        message=f"Pulled {layer_completes} layer(s)",
                    )
        rc = proc.wait()
        if rc != 0:
            raise InstallerError(
                f"docker pull {self.image_ref} failed with exit code {rc}"
            )

        self._record_manifest()
        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=100.0,
            message=f"Pulled {self.image_ref}.",
        )

    def verify(self) -> bool:
        return self.detect() == StepStatus.INSTALLED

    def uninstall(self) -> None:
        """Drop our manifest claim. Does not run ``docker rmi`` — image
        cleanup is the user's call via the global Docker resources page."""
        if self._installer_id:
            try:
                ImageManifest().remove_reference(
                    self.image_ref, self._installer_id
                )
            except Exception as exc:
                logger.warning("Failed to drop manifest reference: %s", exc)

    def storage_inventory(self) -> List[StorageItem]:
        # Images are global — managed via the Docker resources page, not
        # per-installer. Returning an empty list is deliberate.
        return []


# ---------------------------------------------------------------------------
# DockerRun
# ---------------------------------------------------------------------------


class DockerRun(BaseStep):
    """Runs (or restarts) a long-lived container.

    The container is started detached (-d) with a ``--restart`` policy so
    it survives host reboots without OpenJarvis having to supervise it.

    Volumes are declared as :class:`VolumeMount` instances; their host
    paths are auto-created if missing and surfaced in
    :meth:`storage_inventory` so the Storage panel can show sizes.
    """

    def __init__(
        self,
        *,
        container_name: str,
        image_ref: str,
        ports: Optional[Dict[int, int]] = None,
        volumes: Optional[List[VolumeMount]] = None,
        env: Optional[Dict[str, str]] = None,
        restart: str = "unless-stopped",
        command: Optional[Union[str, List[str]]] = None,
    ) -> None:
        self.container_name = container_name
        self.image_ref = image_ref
        self.ports = dict(ports or {})
        self.volumes = list(volumes or [])
        self.env = dict(env or {})
        self.restart = restart
        self.command = command
        self.name = f"docker-run:{container_name}"
        self.description = f"Run container {container_name} from {image_ref}"

    # ----- Detection -----------------------------------------------------

    def _container_state(self) -> Optional[str]:
        """Return Docker's State.Status string or None if the container
        doesn't exist."""
        if not docker_available():
            return None
        try:
            r = _run_docker(
                [
                    "inspect",
                    "--format",
                    "{{.State.Status}}",
                    self.container_name,
                ],
                timeout=10,
                check=False,
            )
            if r.returncode != 0:
                return None
            return r.stdout.strip() or None
        except InstallerError:
            return None

    def detect(self) -> StepStatus:
        state = self._container_state()
        if state is None:
            return StepStatus.NOT_INSTALLED
        if state == "running":
            return StepStatus.INSTALLED
        if state in ("exited", "created", "paused"):
            return StepStatus.PARTIAL
        return StepStatus.UNKNOWN

    # ----- Install -------------------------------------------------------

    def install(self) -> Iterator[Progress]:
        state = self._container_state()
        if state == "running":
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=100.0,
                message=f"Container {self.container_name} already running.",
            )
            return

        if state is not None:
            # Container exists but isn't running — just start it.
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=50.0,
                message=f"Starting existing container {self.container_name}...",
            )
            r = _run_docker(["start", self.container_name], timeout=30)
            if r.returncode != 0:
                raise InstallerError(
                    f"docker start failed: {(r.stderr or '').strip()}"
                )
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=100.0,
                message=f"Container {self.container_name} started.",
            )
            return

        # Create + run fresh.
        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=10.0,
            message=f"Creating container {self.container_name}...",
        )

        args: List[str] = [
            "run",
            "-d",
            "--name",
            self.container_name,
            "--restart",
            self.restart,
        ]
        for vol in self.volumes:
            try:
                # Ensure the parent dir exists. We deliberately don't
                # auto-create the host_path itself: if the recipe wants a
                # file mount, an earlier ConfigFile step should have
                # written it; if it wants a dir mount, the recipe should
                # have set host_path to an existing dir or accept that
                # Docker will create it on first run.
                vol.host_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise InstallerError(
                    f"Failed to prepare host path {vol.host_path}: {exc}"
                ) from exc
            args += ["-v", f"{vol.host_path}:{vol.container_path}"]
        for host_port, container_port in self.ports.items():
            args += ["-p", f"{host_port}:{container_port}"]
        for k, v in self.env.items():
            args += ["-e", f"{k}={v}"]
        args.append(self.image_ref)
        if self.command:
            args += (
                self.command if isinstance(self.command, list) else [self.command]
            )

        r = _run_docker(args, timeout=120)
        if r.returncode != 0:
            raise InstallerError(
                f"docker run failed: {(r.stderr or '').strip()}"
            )
        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=100.0,
            message=f"Container {self.container_name} started.",
        )

    # ----- Verify --------------------------------------------------------

    def verify(self) -> bool:
        return self._container_state() == "running"

    # ----- Uninstall -----------------------------------------------------

    def uninstall(self) -> None:
        """Stop and remove the container. Image is left alone."""
        if not docker_available():
            return
        try:
            _run_docker(["rm", "-f", self.container_name], timeout=30, check=False)
        except InstallerError:
            pass

    # ----- Storage -------------------------------------------------------

    def storage_inventory(self) -> List[StorageItem]:
        items: List[StorageItem] = []
        for vol in self.volumes:
            if not vol.report_storage:
                # Another step (typically ConfigFile) owns this item.
                continue
            size = measure_path_bytes(vol.host_path)
            # Heuristic: if the host path is a single file, classify as
            # CONFIG; otherwise as VOLUME. Either kind is wipeable.
            kind = (
                StorageKind.CONFIG
                if vol.host_path.is_file()
                else StorageKind.VOLUME
            )
            items.append(
                StorageItem(
                    item_id=vol.item_id,
                    kind=kind,
                    description=vol.description,
                    size_bytes=size,
                    wipeability=vol.wipeability,
                    path=vol.host_path,
                )
            )
        return items

    # ----- Wipe lifecycle ------------------------------------------------

    def needs_restart_for_wipe(self, item_ids: set) -> bool:
        my_ids = {v.item_id for v in self.volumes}
        if not (my_ids & set(item_ids)):
            return False
        return self._container_state() == "running"

    def stop(self) -> None:
        if not docker_available():
            return
        try:
            _run_docker(["stop", self.container_name], timeout=30, check=False)
        except InstallerError:
            pass

    def start(self) -> None:
        if not docker_available():
            return
        try:
            _run_docker(["start", self.container_name], timeout=30, check=False)
        except InstallerError:
            pass


# ---------------------------------------------------------------------------
# WaitForHTTP
# ---------------------------------------------------------------------------


class WaitForHTTP(BaseStep):
    """Polls an HTTP endpoint until it returns the expected status.

    Used as the final step after a DockerRun to confirm the service is
    actually serving requests, not just "container started".
    """

    def __init__(
        self,
        url: str,
        *,
        expected_status: int = 200,
        timeout: int = 60,
        interval: float = 2.0,
        what: str = "service",
    ) -> None:
        self.url = url
        self.expected_status = expected_status
        self.timeout = timeout
        self.interval = interval
        self.name = f"wait-for:{what}"
        self.description = f"Wait for {what} at {url}"

    def _check(self) -> bool:
        try:
            import httpx

            r = httpx.get(self.url, timeout=5.0)
            return r.status_code == self.expected_status
        except Exception:
            return False

    def detect(self) -> StepStatus:
        return (
            StepStatus.INSTALLED
            if self._check()
            else StepStatus.NOT_INSTALLED
        )

    def install(self) -> Iterator[Progress]:
        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=0.0,
            message=f"Waiting for {self.url}...",
        )

        start = time.monotonic()
        while True:
            if self._check():
                yield Progress(
                    step_idx=0,
                    step_name=self.name,
                    percent=100.0,
                    message="Service is responding.",
                )
                return

            elapsed = time.monotonic() - start
            if elapsed >= self.timeout:
                raise InstallerError(
                    f"Timed out after {self.timeout}s waiting for {self.url} "
                    f"to return HTTP {self.expected_status}."
                )

            pct = min(95.0, (elapsed / self.timeout) * 100.0)
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=pct,
                message=f"Still waiting ({int(elapsed)}s elapsed)...",
            )
            time.sleep(self.interval)

    def verify(self) -> bool:
        return self._check()


__all__ = [
    "DockerEnvCheck",
    "DockerImage",
    "DockerRun",
    "WaitForHTTP",
    "docker_available",
    "docker_cli_present",
    "docker_installed",
    "list_managed_images",
    "start_docker_desktop",
]
