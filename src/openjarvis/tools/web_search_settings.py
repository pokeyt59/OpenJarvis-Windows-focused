"""User-mutable web_search backend selection — sidecar JSON layer.

Why a sidecar rather than touching ``config.toml`` directly:

- ``config.toml`` is hand-edited by users; writing to it from inside the
  app would either lose their comments + formatting or require a full
  round-trip parse/serialise. The TOML library we already use for
  loading (``tomllib`` / ``tomli``) is read-only.
- The web_search backend is the only setting we currently let the
  *running* app mutate (via the Tools tab in the frontend). One small
  side file is cheaper than a write-safe TOML editor.
- Precedence (highest wins) — env var → sidecar → ``config.toml`` →
  built-in default. The sidecar slots between env-var override and the
  user's static TOML, which is the natural place for "I clicked the
  toggle in the UI" settings.

File location: ``~/.openjarvis/tools/web_search.json``. The directory
is created with ``secure_mkdir`` (0700 on POSIX) so the file shares the
permissions of the rest of the config dir.

Shape: ``{"backend": "...", "searxng_url": "...", "updated_at": "..."}``.
Missing fields are treated as "fall through to the next layer" — the
file isn't a complete config, just an override sheet.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.security.file_utils import secure_mkdir

logger = logging.getLogger(__name__)

# Subdirectory under the config root for tool-level sidecars. Keeping
# it under ``tools/`` leaves room for future per-tool override files
# (e.g. ``tools/code_interpreter.json``) without polluting the root.
_TOOLS_DIR = DEFAULT_CONFIG_DIR / "tools"
_SIDECAR_PATH = _TOOLS_DIR / "web_search.json"

# The valid backend names — duplicated from web_search.py rather than
# imported to avoid a circular import (web_search.py reads this module
# during ``__init__``). If we add a new backend, update both places.
_VALID_BACKENDS = {"tavily", "duckduckgo", "searxng", "auto"}


def _validate_backend_chain(backend: str) -> str:
    """Normalise + validate a comma-separated backend chain.

    Returns the canonical form (lowercase, whitespace-trimmed, dedup'd
    while preserving order). Raises ``ValueError`` on unknown entries —
    the caller (HTTP route) surfaces that as a 400.

    Empty input is allowed (returns ``""``) so the caller can use it as
    a "clear the override" signal without a separate delete endpoint.
    """
    if not backend or not backend.strip():
        return ""
    chain: list[str] = []
    seen: set[str] = set()
    for raw in backend.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in _VALID_BACKENDS:
            raise ValueError(
                f"Unknown backend {name!r}; valid: "
                + ", ".join(sorted(_VALID_BACKENDS - {"auto"}))
                + ', "auto"',
            )
        if name in seen:
            continue
        seen.add(name)
        chain.append(name)
    return ",".join(chain)


def load_web_search_settings() -> Optional[dict]:
    """Return the sidecar contents, or ``None`` if no override is set.

    A missing file is the normal case — most users never touch this and
    fall through to ``config.toml``. We treat any I/O or JSON error as
    "no override" rather than blowing up; the worst case is the tool
    quietly uses its default, which the user will see in the UI.
    """
    try:
        if not _SIDECAR_PATH.exists():
            return None
        raw = _SIDECAR_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as exc:
        logger.debug("web_search sidecar unreadable (%s) — ignoring", exc)
        return None


def save_web_search_settings(
    *,
    backend: Optional[str] = None,
    searxng_url: Optional[str] = None,
) -> dict:
    """Persist a backend override. Returns the merged-on-disk state.

    Both args are optional; only the ones you pass get written. Pass
    ``backend=""`` to clear the override and let config.toml win again.
    Pass ``searxng_url=""`` to clear the URL override the same way.

    Atomic write via a temp file in the same directory — protects
    against corruption if the process is killed mid-write.
    """
    secure_mkdir(_TOOLS_DIR)

    # Start from whatever is already on disk so partial updates merge
    # cleanly rather than wiping the unrelated field.
    current = load_web_search_settings() or {}
    if backend is not None:
        canonical = _validate_backend_chain(backend)
        if canonical:
            current["backend"] = canonical
        else:
            current.pop("backend", None)
    if searxng_url is not None:
        url = searxng_url.strip()
        if url:
            current["searxng_url"] = url
        else:
            current.pop("searxng_url", None)
    current["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # If we cleared everything, just remove the file so ``load`` sees
    # a clean state (otherwise we leave a stub with just ``updated_at``
    # which would still count as "override present" in some paths).
    if not any(k in current for k in ("backend", "searxng_url")):
        try:
            _SIDECAR_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return {}

    # Atomic write: temp file → rename. ``delete=False`` because we
    # need to close the FD before renaming on Windows.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=_TOOLS_DIR,
        prefix=".web_search.json.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        json.dump(current, fh, indent=2, sort_keys=True)
        tmp_path = Path(fh.name)
    os.replace(tmp_path, _SIDECAR_PATH)

    # POSIX: lock down the file. No-op on Windows.
    try:
        os.chmod(_SIDECAR_PATH, 0o600)
    except OSError:
        pass

    return current


def clear_web_search_settings() -> None:
    """Delete the sidecar entirely. Equivalent to ``save(backend="", searxng_url="")``."""
    try:
        _SIDECAR_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("web_search sidecar unlink failed (%s) — ignoring", exc)


# ---------------------------------------------------------------------------
# Helpers used by WebSearchTool to consult the sidecar before falling
# through to config.toml. Live here (rather than inside web_search.py)
# so the router can update + verify settings without importing the tool.
# ---------------------------------------------------------------------------


def get_effective_backend(*, env_value: Optional[str], toml_value: str) -> str:
    """Pick the backend chain to use, applying the documented precedence.

    Order: env → sidecar → toml → "auto". Returns the raw string (the
    tool still passes it through ``_resolve_backend_chain``).
    """
    if env_value:
        return env_value
    sidecar = load_web_search_settings() or {}
    if isinstance(sidecar.get("backend"), str) and sidecar["backend"]:
        return sidecar["backend"]
    return toml_value or "auto"


def get_effective_searxng_url(
    *, env_value: Optional[str], toml_value: str
) -> str:
    """Pick the SearXNG URL to use, mirroring the backend precedence."""
    if env_value:
        return env_value
    sidecar = load_web_search_settings() or {}
    if isinstance(sidecar.get("searxng_url"), str) and sidecar["searxng_url"]:
        return sidecar["searxng_url"]
    return toml_value or "http://127.0.0.1:8888"


__all__ = [
    "load_web_search_settings",
    "save_web_search_settings",
    "clear_web_search_settings",
    "get_effective_backend",
    "get_effective_searxng_url",
]
