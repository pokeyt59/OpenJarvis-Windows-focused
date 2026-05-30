"""Installer scaffolding for third-party dependencies.

Provides a small, reusable abstraction for connectors that need to install
something on the host machine — Docker containers, downloaded models, CLI
binaries, etc.

Public surface:

- :class:`Installer` — a sequence of :class:`InstallerStep` walked to reach
  ready state.
- :class:`StorageItem`, :class:`StorageReport`, :class:`Wipeability` —
  describe what persistent state an installer owns and how safe it is to
  delete.
- :class:`Progress` events — yielded by ``install()`` so frontends can
  render a step-by-step UI.
- :func:`get_installer` / :func:`register_installer` — registry lookup by
  ``installer_id``.

Docker images are NOT considered per-installer storage. They are shared
system resources; manage them via the global Docker resources page.
"""

from openjarvis.installers.base import (
    Installer,
    InstallerError,
    InstallerStep,
    OverallStatus,
    Progress,
    ProgressLevel,
    StepStatus,
    StorageItem,
    StorageKind,
    StorageReport,
    VolumeMount,
    WipeRefused,
    Wipeability,
)
from openjarvis.installers.registry import (
    get_installer,
    list_installers,
    register_installer,
)

__all__ = [
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
    "get_installer",
    "list_installers",
    "register_installer",
]
