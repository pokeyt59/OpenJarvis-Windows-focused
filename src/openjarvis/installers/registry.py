"""Installer registry — maps ``installer_id`` to :class:`Installer`.

Recipes register themselves on import. The frontend / API layer looks
installers up by id (``"web_search.searxng"``, ``"vector_db.qdrant"``,
etc.) and dispatches install / wipe / status calls.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from openjarvis.installers.base import Installer

logger = logging.getLogger(__name__)

_registry: Dict[str, Installer] = {}
_lock = threading.Lock()


def register_installer(installer: Installer) -> None:
    """Add an installer to the registry. Idempotent.

    If an installer with the same id is already registered, the new
    instance replaces it and a warning is logged (catches double-import
    bugs without breaking the app).
    """
    with _lock:
        if installer.installer_id in _registry:
            logger.warning(
                "Replacing already-registered installer %s",
                installer.installer_id,
            )
        _registry[installer.installer_id] = installer


def get_installer(installer_id: str) -> Optional[Installer]:
    """Return the installer for ``installer_id`` or None if missing."""
    _ensure_installers_loaded()
    with _lock:
        return _registry.get(installer_id)


def list_installers() -> List[str]:
    """Return sorted list of registered installer ids."""
    _ensure_installers_loaded()
    with _lock:
        return sorted(_registry.keys())


def _clear_registry_for_tests() -> None:
    """Reset the registry. Test-only — not part of the public API."""
    with _lock:
        _registry.clear()


def _ensure_installers_loaded() -> None:
    """Auto-import known recipe modules so they self-register.

    Each recipe import is wrapped in try/except so a single broken recipe
    can't take down the whole registry — mirrors the connector
    auto-registration pattern in ``openjarvis.connectors.__init__``.
    """
    # Already populated? Nothing to do.
    if _registry:
        return

    try:
        import openjarvis.installers.recipes.searxng  # noqa: F401
    except Exception as exc:
        logger.debug("Failed to import searxng recipe: %s", exc)


__all__ = [
    "get_installer",
    "list_installers",
    "register_installer",
]
