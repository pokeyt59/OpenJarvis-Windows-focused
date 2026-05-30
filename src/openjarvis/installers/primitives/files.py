"""File-based installer primitives — for v1, just ConfigFile.

FileDownload (for model weights / binaries) and BinaryInstall (for native
CLI tools like Ollama) are deferred until a recipe actually needs them.
Adding them later doesn't break anything because the protocol is open.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List, Optional

from openjarvis.installers.base import (
    BaseStep,
    InstallerError,
    Progress,
    StepStatus,
    StorageItem,
    StorageKind,
    Wipeability,
    measure_path_bytes,
)

logger = logging.getLogger(__name__)


class ConfigFile(BaseStep):
    """Writes a configuration file to disk.

    Idempotent: if the file exists with the same content, install() is a
    no-op. Otherwise the file is (re-)written. The installer records the
    file in its storage inventory so the per-connector Storage panel can
    show its size and offer "Restore defaults" (which is just a wipe +
    re-install of this step).
    """

    def __init__(
        self,
        *,
        path: Path,
        content: str,
        item_id: str,
        description: str = "Configuration file",
        mode: int = 0o644,
        encoding: str = "utf-8",
    ) -> None:
        self.path = Path(path)
        self.content = content
        self.item_id = item_id
        self.config_description = description
        self.mode = mode
        self.encoding = encoding
        self.name = f"config-file:{self.path.name}"
        self.description = f"Write {self.path}"

    def detect(self) -> StepStatus:
        if not self.path.exists():
            return StepStatus.NOT_INSTALLED
        try:
            existing = self.path.read_text(encoding=self.encoding)
        except (OSError, UnicodeDecodeError):
            return StepStatus.PARTIAL  # exists but unreadable
        if existing == self.content:
            return StepStatus.INSTALLED
        # File exists but content differs — treat as PARTIAL so install()
        # rewrites it on re-run, but the user knows it's "their" file.
        return StepStatus.PARTIAL

    def install(self) -> Iterator[Progress]:
        state = self.detect()
        if state == StepStatus.INSTALLED:
            yield Progress(
                step_idx=0,
                step_name=self.name,
                percent=100.0,
                message=f"{self.path.name} already matches expected content.",
            )
            return

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.content, encoding=self.encoding)
            # chmod is best-effort on Windows (ignored), required on POSIX.
            try:
                self.path.chmod(self.mode)
            except OSError:
                pass
        except OSError as exc:
            raise InstallerError(
                f"Failed to write {self.path}: {exc}"
            ) from exc

        yield Progress(
            step_idx=0,
            step_name=self.name,
            percent=100.0,
            message=f"Wrote {self.path}.",
        )

    def verify(self) -> bool:
        return self.path.exists()

    def uninstall(self) -> None:
        """Leave the file alone — it may contain user customizations.

        The user can wipe it explicitly via the Storage panel (which
        appears as 'Restore defaults' in the UI thanks to the REPLACEABLE
        wipeability).
        """
        return None

    def storage_inventory(self) -> List[StorageItem]:
        return [
            StorageItem(
                item_id=self.item_id,
                kind=StorageKind.CONFIG,
                description=self.config_description,
                size_bytes=measure_path_bytes(self.path),
                wipeability=Wipeability.REPLACEABLE,
                path=self.path,
            )
        ]


__all__ = ["ConfigFile"]
