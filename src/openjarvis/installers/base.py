"""Core installer protocols, types, and orchestration.

This module is pure-Python with no external dependencies — primitives that
do actual work (Docker, file downloads, etc.) live in
``openjarvis.installers.primitives``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums / lightweight types
# ---------------------------------------------------------------------------


class StepStatus(str, Enum):
    """The state of a single installer step."""

    NOT_INSTALLED = "not_installed"
    PARTIAL = "partial"
    INSTALLED = "installed"
    BROKEN = "broken"            # installed but failing verify()
    UNKNOWN = "unknown"          # detect() raised


class OverallStatus(str, Enum):
    """Aggregate status across all steps in an installer."""

    NOT_INSTALLED = "not_installed"   # no steps installed
    PARTIAL = "partial"               # some installed, some not
    READY = "ready"                   # all installed + verified
    BROKEN = "broken"                 # at least one verifiable step failed
    UNKNOWN = "unknown"


class ProgressLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class Wipeability(str, Enum):
    """How safe it is for the user to wipe a piece of storage.

    The UI bases its confirmation pattern on this:
      EPHEMERAL    — one-click button, no prompt
      REPLACEABLE  — simple confirm dialog ("are you sure?")
      IRRECOVERABLE — type-to-confirm modal; wipe() refuses without force=True
    """

    EPHEMERAL = "ephemeral"
    REPLACEABLE = "replaceable"
    IRRECOVERABLE = "irrecoverable"


class StorageKind(str, Enum):
    """What kind of storage this item represents.

    Note ``image`` is intentionally absent: Docker images are global
    resources managed via the Docker resources page, not per-installer.
    """

    VOLUME = "volume"     # bind-mount directory (container data)
    CONFIG = "config"     # configuration file/dir on host
    MODEL = "model"       # downloaded model weights, large blobs
    LOG = "log"           # installer or service logs


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    """A single progress event yielded during install()/wipe()."""

    step_idx: int               # 0-based index into Installer.steps
    step_name: str
    percent: float              # 0..100 within this step
    message: str
    level: ProgressLevel = ProgressLevel.INFO

    def __post_init__(self) -> None:
        # Clamp percent to [0, 100] without raising — progress is advisory.
        if not 0.0 <= self.percent <= 100.0:
            object.__setattr__(self, "percent", max(0.0, min(100.0, self.percent)))


@dataclass(frozen=True)
class StorageItem:
    """A single piece of persistent storage owned by an installer step."""

    item_id: str                # "searxng.config" — stable across installs
    kind: StorageKind
    description: str            # human-readable: "SearXNG configuration"
    size_bytes: int
    wipeability: Wipeability
    path: Optional[Path] = None  # host path; None for opaque resources


@dataclass(frozen=True)
class StorageReport:
    """Aggregated storage report for a whole installer."""

    installer_id: str
    items: List[StorageItem]
    total_bytes: int
    by_kind: dict       # StorageKind -> total_bytes for that kind


@dataclass(frozen=True)
class VolumeMount:
    """Declarative description of a Docker bind-mount.

    The ``wipeability`` defaults to IRRECOVERABLE so a recipe author who
    forgets to classify a mount gets the safest behavior (type-to-confirm
    required to wipe).

    Set ``report_storage=False`` when another step in the recipe owns the
    storage (e.g. a ConfigFile step writes the file before DockerRun
    mounts it). DockerRun will still stop itself for wipes of this item,
    but won't duplicate the file in the per-connector Storage panel.
    """

    host_path: Path
    container_path: str
    item_id: str
    description: str
    wipeability: Wipeability = Wipeability.IRRECOVERABLE
    report_storage: bool = True


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InstallerError(Exception):
    """Raised when an installer step fails.

    Steps may optionally attach a ``link`` (``{"label": str, "url": str}``)
    that the frontend renders as a clickable button alongside the error
    message — used for "we can't auto-install Docker, here's where to
    download it" style errors so the user has one click to fix the problem
    instead of copy-pasting a URL out of an error string.

    The link is keyword-only so existing callers
    (``raise InstallerError("...")``) keep working.
    """

    def __init__(
        self,
        message: str,
        *,
        link: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.link = link


class WipeRefused(Exception):
    """Raised when a wipe is refused because an IRRECOVERABLE item was
    requested without force=True (or the right confirm_phrase)."""


# ---------------------------------------------------------------------------
# Step protocol
# ---------------------------------------------------------------------------


class InstallerStep(Protocol):
    """The contract every primitive must implement.

    Steps are expected to be idempotent — ``install()`` should be safe to
    re-run on a partially-installed system. Use ``detect()`` to skip work
    when the desired state is already reached.
    """

    name: str
    description: str

    def detect(self) -> StepStatus:
        """Inspect the system and report current state. No mutations."""

    def install(self) -> Iterator[Progress]:
        """Drive the system toward the installed state.

        Yields :class:`Progress` events as work proceeds. Must be safe to
        run when already INSTALLED (yield a single 100% INFO and return).
        Raises :class:`InstallerError` on failure.
        """

    def verify(self) -> bool:
        """Confirm post-install health. May be more thorough than detect()."""

    def uninstall(self) -> None:
        """Reverse the install. Idempotent; safe to call when not installed."""

    def storage_inventory(self) -> List[StorageItem]:
        """List persistent state this step owns. Default: empty."""

    # ----- Wipe lifecycle hooks (default: no-op) ------------------------

    def needs_restart_for_wipe(self, item_ids: set) -> bool:
        """True if wiping any of these items requires stopping this step
        first (e.g. a running container holding a bind-mount open)."""

    def stop(self) -> None:
        """Stop any runtime resources owned by this step. Idempotent."""

    def start(self) -> None:
        """Re-start runtime resources after a wipe. Idempotent."""


# ---------------------------------------------------------------------------
# Default step base class
# ---------------------------------------------------------------------------


class BaseStep:
    """Convenience base class implementing the no-op defaults.

    Subclasses MUST implement at minimum: ``name``, ``description``,
    ``detect()``, ``install()``. ``verify()`` defaults to checking that
    ``detect()`` returns INSTALLED.
    """

    name: str = "unnamed-step"
    description: str = ""

    def detect(self) -> StepStatus:  # pragma: no cover — abstract default
        return StepStatus.UNKNOWN

    def install(self) -> Iterator[Progress]:  # pragma: no cover
        raise NotImplementedError

    def verify(self) -> bool:
        try:
            return self.detect() == StepStatus.INSTALLED
        except Exception:
            return False

    def uninstall(self) -> None:
        return None

    def storage_inventory(self) -> List[StorageItem]:
        return []

    def needs_restart_for_wipe(self, item_ids: set) -> bool:
        return False

    def stop(self) -> None:
        return None

    def start(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Installer orchestrator
# ---------------------------------------------------------------------------


@dataclass
class Installer:
    """A sequence of steps that, run in order, install a third-party thing.

    Steps run sequentially. If one fails, subsequent steps are skipped and
    the failure is propagated. Each step's install() is expected to be
    idempotent, so the recommended recovery path is "fix the underlying
    issue and re-run".
    """

    installer_id: str            # "web_search.searxng"
    display_name: str
    description: str = ""
    steps: List[InstallerStep] = field(default_factory=list)
    estimated_total_seconds: int = 60      # for UI ETA
    estimated_download_mb: int = 0         # for UI "this will use ~N MB"

    def __post_init__(self) -> None:
        """Wire each step that wants to know its owning installer id.

        Primitives that need to track per-installer state (e.g. DockerImage
        recording into the image manifest) implement a ``_bind`` method we
        invoke here. Steps without ``_bind`` are skipped silently.
        """
        for step in self.steps:
            bind = getattr(step, "_bind", None)
            if callable(bind):
                try:
                    bind(self.installer_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to _bind step %s to %s: %s",
                        getattr(step, "name", "?"),
                        self.installer_id,
                        exc,
                    )

    # ----- Status -------------------------------------------------------

    def status(self) -> OverallStatus:
        if not self.steps:
            return OverallStatus.READY
        statuses: List[StepStatus] = []
        for step in self.steps:
            try:
                statuses.append(step.detect())
            except Exception as exc:
                logger.warning(
                    "detect() raised for %s/%s: %s",
                    self.installer_id,
                    getattr(step, "name", "?"),
                    exc,
                )
                statuses.append(StepStatus.UNKNOWN)

        if all(s == StepStatus.INSTALLED for s in statuses):
            return OverallStatus.READY
        if any(s == StepStatus.BROKEN for s in statuses):
            return OverallStatus.BROKEN
        if all(s == StepStatus.NOT_INSTALLED for s in statuses):
            return OverallStatus.NOT_INSTALLED
        if any(s == StepStatus.UNKNOWN for s in statuses):
            return OverallStatus.UNKNOWN
        return OverallStatus.PARTIAL

    def step_statuses(self) -> List[StepStatus]:
        out: List[StepStatus] = []
        for step in self.steps:
            try:
                out.append(step.detect())
            except Exception:
                out.append(StepStatus.UNKNOWN)
        return out

    # ----- Install ------------------------------------------------------

    def run(self) -> Iterator[Progress]:
        """Execute all steps in order, yielding Progress.

        Stops on the first :class:`InstallerError`. After raising, the
        installer can be re-run — each step's install() is idempotent.
        """
        for idx, step in enumerate(self.steps):
            try:
                current_state = step.detect()
            except Exception:
                current_state = StepStatus.UNKNOWN

            if current_state == StepStatus.INSTALLED:
                yield Progress(
                    step_idx=idx,
                    step_name=step.name,
                    percent=100.0,
                    message=f"{step.name}: already installed, skipping",
                )
                continue

            try:
                for event in step.install():
                    # Re-emit with the correct step_idx in case the step
                    # constructed Progress with a placeholder index.
                    yield Progress(
                        step_idx=idx,
                        step_name=step.name,
                        percent=event.percent,
                        message=event.message,
                        level=event.level,
                    )
            except InstallerError as exc:
                yield Progress(
                    step_idx=idx,
                    step_name=step.name,
                    percent=0.0,
                    message=str(exc),
                    level=ProgressLevel.ERROR,
                )
                raise

            if not step.verify():
                msg = f"{step.name}: install completed but verify() failed"
                yield Progress(
                    step_idx=idx,
                    step_name=step.name,
                    percent=100.0,
                    message=msg,
                    level=ProgressLevel.ERROR,
                )
                raise InstallerError(msg)

    # ----- Storage ------------------------------------------------------

    def storage_inventory(self) -> StorageReport:
        """Aggregate storage items across steps, deduplicating by item_id.

        Steps earlier in ``self.steps`` win when there's a duplicate — the
        convention is that owning steps (e.g. ConfigFile) come before
        mounting steps (e.g. DockerRun). Mounting steps should also set
        ``VolumeMount.report_storage=False`` for items they don't own, so
        most of the time this dedup never triggers.
        """
        seen: set = set()
        items: List[StorageItem] = []
        for step in self.steps:
            try:
                step_items = step.storage_inventory()
            except Exception as exc:
                logger.warning(
                    "storage_inventory() raised for %s/%s: %s",
                    self.installer_id,
                    getattr(step, "name", "?"),
                    exc,
                )
                continue
            for it in step_items:
                if it.item_id in seen:
                    continue
                seen.add(it.item_id)
                items.append(it)

        by_kind: dict = defaultdict(int)
        for it in items:
            by_kind[it.kind] += it.size_bytes

        return StorageReport(
            installer_id=self.installer_id,
            items=items,
            total_bytes=sum(it.size_bytes for it in items),
            by_kind=dict(by_kind),
        )

    # ----- Wipe ---------------------------------------------------------

    def wipe(
        self,
        item_ids: Iterable[str],
        *,
        force: bool = False,
        restart_after: bool = True,
    ) -> Iterator[Progress]:
        """Wipe the requested items.

        Lifecycle:
          1. Pre-flight: refuse if any requested item is IRRECOVERABLE
             and ``force`` is False.
          2. For each step that needs a stop before wipe (e.g. running
             container holding a bind-mount), call ``step.stop()``.
          3. Delete each item's path.
          4. If ``restart_after`` is True, ``start()`` any steps we stopped.

        Yields :class:`Progress` events so the UI can show progress.
        """
        wanted = set(item_ids)
        if not wanted:
            return

        # Locate items + the steps that own them.
        items_by_id: dict = {}
        owners_by_id: dict = {}
        for step in self.steps:
            try:
                step_items = step.storage_inventory()
            except Exception:
                continue
            for it in step_items:
                if it.item_id in wanted:
                    items_by_id[it.item_id] = it
                    owners_by_id[it.item_id] = step

        missing = wanted - set(items_by_id)
        if missing:
            raise WipeRefused(f"Unknown item ids: {sorted(missing)}")

        # Pre-flight: irrecoverable check.
        if not force:
            blocked = [
                it for it in items_by_id.values()
                if it.wipeability == Wipeability.IRRECOVERABLE
            ]
            if blocked:
                ids = sorted(it.item_id for it in blocked)
                raise WipeRefused(
                    f"Refusing to wipe IRRECOVERABLE items without force=True: {ids}"
                )

        # Determine which steps need to be stopped first.
        steps_to_restart: List[InstallerStep] = []
        for step in self.steps:
            try:
                step_items = {i.item_id for i in step.storage_inventory()}
            except Exception:
                step_items = set()
            affected = step_items & wanted
            if affected and step.needs_restart_for_wipe(affected):
                yield Progress(
                    step_idx=self.steps.index(step),
                    step_name=step.name,
                    percent=50.0,
                    message=f"Stopping {step.name} before wipe",
                )
                try:
                    step.stop()
                except Exception as exc:
                    raise InstallerError(
                        f"Failed to stop {step.name} before wipe: {exc}"
                    ) from exc
                steps_to_restart.append(step)

        # Delete each item's storage.
        total = len(items_by_id)
        for n, (item_id, item) in enumerate(items_by_id.items(), start=1):
            step = owners_by_id[item_id]
            step_idx = self.steps.index(step)
            yield Progress(
                step_idx=step_idx,
                step_name=step.name,
                percent=(n / total) * 100.0,
                message=f"Wiping {item.description}",
            )
            _delete_path(item.path)

        # Restart what we stopped.
        if restart_after:
            for step in steps_to_restart:
                yield Progress(
                    step_idx=self.steps.index(step),
                    step_name=step.name,
                    percent=100.0,
                    message=f"Restarting {step.name}",
                )
                try:
                    step.start()
                except Exception as exc:
                    raise InstallerError(
                        f"Failed to restart {step.name} after wipe: {exc}"
                    ) from exc

    # ----- Uninstall ----------------------------------------------------

    def uninstall(self) -> None:
        """Reverse the install. Walks steps in reverse order."""
        for step in reversed(self.steps):
            try:
                step.uninstall()
            except Exception as exc:
                logger.warning(
                    "uninstall() raised for %s/%s: %s",
                    self.installer_id,
                    getattr(step, "name", "?"),
                    exc,
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _delete_path(path: Optional[Path]) -> None:
    """Delete a file or directory if it exists. No-op if path is None.

    Uses ``shutil.rmtree`` for directories and ``Path.unlink`` for files.
    Failures are logged but not raised — wipe is best-effort.
    """
    if path is None:
        return
    if not path.exists():
        return
    try:
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink()
    except Exception as exc:
        logger.warning("Failed to delete %s: %s", path, exc)


def measure_path_bytes(path: Optional[Path]) -> int:
    """Return the size in bytes of a file or directory, recursive.

    Returns 0 if path is None or doesn't exist. Symlinks aren't followed
    to avoid double-counting and cycle traps.
    """
    if path is None or not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file() and not f.is_symlink():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


__all__ = [
    "BaseStep",
    "Installer",
    "InstallerError",
    "InstallerStep",
    "OverallStatus",
    "Progress",
    "ProgressLevel",
    "StepStatus",
    "StorageItem",
    "StorageKind",
    "StorageReport",
    "VolumeMount",
    "WipeRefused",
    "Wipeability",
    "measure_path_bytes",
]
