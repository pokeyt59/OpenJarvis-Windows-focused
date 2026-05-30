"""Tracks which Docker images OpenJarvis has pulled and which installers
reference each one.

We can't add labels to images that Docker pulled (labels are baked at
build time), so we maintain a sidecar JSON manifest. This lets the global
Docker resources page show usage attribution ("searxng/searxng — used by
SearXNG, Qdrant") without having to introspect every installer's state.

File: ``~/.openjarvis/installers/managed_images.json``

Shape::

    {
        "searxng/searxng:latest": ["web_search.searxng"],
        "qdrant/qdrant:v1.7":     ["vector_db.qdrant"]
    }
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _default_manifest_path() -> Path:
    """Return the default manifest location.

    Resolves lazily so test fixtures can monkey-patch the underlying
    config dir without import-order headaches.
    """
    try:
        from openjarvis.core.config import DEFAULT_CONFIG_DIR

        base = Path(DEFAULT_CONFIG_DIR)
    except Exception:
        base = Path.home() / ".openjarvis"
    return base / "installers" / "managed_images.json"


class ImageManifest:
    """JSON-backed image reference manifest. Thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else _default_manifest_path()

    def _read(self) -> Dict[str, List[str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            # Coerce to {str: list[str]}
            out: Dict[str, List[str]] = {}
            for k, v in data.items():
                if isinstance(v, list):
                    out[str(k)] = [str(x) for x in v]
            return out
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read manifest %s: %s", self.path, exc)
            return {}

    def _write(self, data: Dict[str, List[str]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to write manifest %s: %s", self.path, exc)

    # ----- Public API -----------------------------------------------------

    def add_reference(self, image_ref: str, installer_id: str) -> None:
        """Record that ``installer_id`` uses ``image_ref``. Idempotent."""
        with _lock:
            data = self._read()
            refs = data.setdefault(image_ref, [])
            if installer_id not in refs:
                refs.append(installer_id)
                refs.sort()
                self._write(data)

    def remove_reference(self, image_ref: str, installer_id: str) -> None:
        """Drop ``installer_id``'s claim on ``image_ref``. If no claims
        remain, removes the image entry entirely."""
        with _lock:
            data = self._read()
            refs = data.get(image_ref, [])
            if installer_id in refs:
                refs = [r for r in refs if r != installer_id]
                if refs:
                    data[image_ref] = refs
                else:
                    data.pop(image_ref, None)
                self._write(data)

    def references_for(self, image_ref: str) -> List[str]:
        """Return installer ids currently claiming ``image_ref``."""
        with _lock:
            return list(self._read().get(image_ref, []))

    def all_managed(self) -> Dict[str, List[str]]:
        """Return a snapshot of the full manifest."""
        with _lock:
            return dict(self._read())

    def is_managed(self, image_ref: str) -> bool:
        """True if any installer claims this image."""
        with _lock:
            return bool(self._read().get(image_ref))


__all__ = ["ImageManifest"]
