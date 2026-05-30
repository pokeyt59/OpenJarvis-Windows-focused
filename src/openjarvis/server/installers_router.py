"""FastAPI router for ``/v1/installers`` — third-party install lifecycle.

Endpoints
---------
- ``GET  /v1/installers``                        — list registered installers
- ``GET  /v1/installers/{id}/status``            — current status (cached)
- ``POST /v1/installers/{id}/run``               — run install, SSE Progress
- ``GET  /v1/installers/{id}/storage``           — StorageReport
- ``POST /v1/installers/{id}/storage/refresh``   — bust the status cache
- ``POST /v1/installers/{id}/wipe``              — wipe storage items
- ``GET  /v1/docker/resources``                  — managed Docker images + usage

Design notes
------------
- Routes are wrapped in ``create_installers_router()`` so the FastAPI
  import is deferred (matches connectors_router pattern). The factory
  binds endpoint closures over a per-process status cache.
- ``/run`` returns ``text/event-stream`` — each Progress event becomes
  one ``data: <json>\\n\\n`` line. The frontend consumes via
  ``EventSource``; we explicitly close the stream on completion or
  the first ``InstallerError`` (no auto-retry — installer failures
  need user intervention).
- ``/wipe`` requires a server-side ``confirm_phrase`` check for any
  IRRECOVERABLE item. The expected phrase is ``f"wipe {installer_id}"``
  (lowercase, exact). This is a defense in depth — the frontend should
  also gate the UI — but the server enforces the contract regardless.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-process cache for /status, keyed by installer_id → (timestamp, payload).
# 30-second TTL is enough that the frontend can poll cheaply without
# wedging the UI thread on Docker subprocess calls.
_STATUS_CACHE_TTL_SECONDS = 30
_status_cache: Dict[str, tuple] = {}
_status_cache_lock = threading.Lock()


def _make_status_payload(installer_id: str) -> Dict[str, Any]:
    """Build the status dict — called both fresh and from cache."""
    from openjarvis.installers import get_installer

    installer = get_installer(installer_id)
    if installer is None:
        raise KeyError(installer_id)
    overall = installer.status()
    step_statuses = installer.step_statuses()
    return {
        "installer_id": installer.installer_id,
        "display_name": installer.display_name,
        "description": installer.description,
        "status": overall.value,
        "estimated_total_seconds": installer.estimated_total_seconds,
        "estimated_download_mb": installer.estimated_download_mb,
        "steps": [
            {"name": step.name, "status": status.value}
            for step, status in zip(installer.steps, step_statuses, strict=False)
        ],
    }


def _cached_status(installer_id: str, *, force_refresh: bool = False) -> Dict[str, Any]:
    """Return a cached status payload, refreshing on TTL or force."""
    now = time.time()
    if not force_refresh:
        with _status_cache_lock:
            entry = _status_cache.get(installer_id)
            if entry is not None:
                ts, payload = entry
                if now - ts < _STATUS_CACHE_TTL_SECONDS:
                    return payload

    payload = _make_status_payload(installer_id)
    with _status_cache_lock:
        _status_cache[installer_id] = (now, payload)
    return payload


def _invalidate_status_cache(installer_id: Optional[str] = None) -> None:
    """Clear cached status — called after run/wipe to force a fresh read."""
    with _status_cache_lock:
        if installer_id is None:
            _status_cache.clear()
        else:
            _status_cache.pop(installer_id, None)


# ---------------------------------------------------------------------------
# Pydantic request models — module-level so FastAPI's type resolution works
# ---------------------------------------------------------------------------

try:
    from pydantic import BaseModel as _BaseModel
    from pydantic import Field as _Field

    class WipeRequest(_BaseModel):
        """Payload for POST /wipe.

        ``item_ids`` is the list of storage_inventory item_ids to delete.
        ``confirm_phrase`` is the user-typed phrase (required when any
        targeted item is IRRECOVERABLE — see ``force`` below for the
        bypass used by automated tests).
        ``force`` skips the IRRECOVERABLE guard entirely. NEVER expose
        this in the frontend — it exists for scripted recovery.
        ``restart_after`` controls whether stopped steps are restarted
        after the wipe (default true; set false for full-reset flows).
        """

        item_ids: List[str] = _Field(default_factory=list)
        confirm_phrase: str = ""
        force: bool = False
        restart_after: bool = True

except ImportError:
    WipeRequest = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_installers_router():
    """Return an APIRouter with the installer lifecycle endpoints."""
    try:
        from fastapi import APIRouter, HTTPException
        from fastapi.responses import StreamingResponse
    except ImportError as exc:
        raise ImportError(
            "fastapi is required for the installers router"
        ) from exc

    if WipeRequest is None:
        raise ImportError("pydantic is required for the installers router")

    from openjarvis.installers import (
        InstallerError,
        WipeRefused,
        get_installer,
        list_installers,
    )

    router = APIRouter(prefix="/v1/installers", tags=["installers"])

    # ------------------------------------------------------------------
    # Listing + status
    # ------------------------------------------------------------------

    @router.get("")
    async def list_all():
        """List every registered installer with its current status."""
        ids = list_installers()
        results: List[Dict[str, Any]] = []
        for iid in ids:
            try:
                results.append(_cached_status(iid))
            except Exception:
                # Don't fail the whole list because one installer is
                # broken — return a minimal stub so the UI still shows it.
                inst = get_installer(iid)
                results.append({
                    "installer_id": iid,
                    "display_name": inst.display_name if inst else iid,
                    "status": "unknown",
                    "steps": [],
                })
        return {"installers": results}

    @router.get("/{installer_id}/status")
    async def status(installer_id: str):
        """Cached status (30s TTL). Use /status/refresh to bust."""
        try:
            return _cached_status(installer_id)
        except KeyError:
            raise HTTPException(404, f"Installer '{installer_id}' not found")

    @router.post("/{installer_id}/status/refresh")
    async def refresh_status(installer_id: str):
        """Bust the status cache and return a fresh read."""
        if get_installer(installer_id) is None:
            raise HTTPException(404, f"Installer '{installer_id}' not found")
        _invalidate_status_cache(installer_id)
        return _cached_status(installer_id, force_refresh=True)

    # ------------------------------------------------------------------
    # Install (SSE)
    # ------------------------------------------------------------------

    @router.post("/{installer_id}/run")
    async def run_install(installer_id: str):
        """Run the installer, streaming Progress events as SSE.

        The response is ``text/event-stream``; each event is one JSON
        object on its own ``data:`` line. On completion we send a final
        ``event: done`` line so the client knows to stop reading.
        On failure we send ``event: error`` with the message and close.
        """
        installer = get_installer(installer_id)
        if installer is None:
            raise HTTPException(404, f"Installer '{installer_id}' not found")

        def _gen():
            try:
                for progress in installer.run():
                    payload = json.dumps({
                        "step_idx": progress.step_idx,
                        "step_name": progress.step_name,
                        "percent": progress.percent,
                        "message": progress.message,
                        "level": progress.level.value,
                    })
                    yield f"data: {payload}\n\n"
                # Send a final completion marker. The frontend uses this
                # to switch the install card from "running" to "ready"
                # without waiting for a /status refresh round-trip.
                yield "event: done\ndata: {}\n\n"
            except InstallerError as exc:
                # Forward ``link`` (if any) so the UI can render a
                # clickable button — e.g. the Docker-missing step
                # attaches an "Install Docker Desktop" link.
                err_payload = {"error": str(exc)}
                link = getattr(exc, "link", None)
                if link:
                    err_payload["link"] = link
                yield f"event: error\ndata: {json.dumps(err_payload)}\n\n"
            except Exception as exc:  # noqa: BLE001
                # Defensive: any uncaught exception inside a step should
                # not leak the stack trace to the client.
                logger.exception("Installer %s blew up", installer_id)
                yield f"event: error\ndata: {json.dumps({'error': f'Internal error: {type(exc).__name__}'})}\n\n"
            finally:
                # Force a fresh /status next time the frontend asks.
                _invalidate_status_cache(installer_id)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # Storage inventory + wipe
    # ------------------------------------------------------------------

    @router.get("/{installer_id}/storage")
    async def storage(installer_id: str):
        """Return the StorageReport for the installer."""
        installer = get_installer(installer_id)
        if installer is None:
            raise HTTPException(404, f"Installer '{installer_id}' not found")
        report = installer.storage_inventory()
        return {
            "installer_id": report.installer_id,
            "total_bytes": report.total_bytes,
            "by_kind": {k.value: v for k, v in report.by_kind.items()},
            "items": [
                {
                    "item_id": it.item_id,
                    "kind": it.kind.value,
                    "description": it.description,
                    "size_bytes": it.size_bytes,
                    "wipeability": it.wipeability.value,
                    "path": str(it.path) if it.path else None,
                }
                for it in report.items
            ],
        }

    @router.post("/{installer_id}/wipe")
    async def wipe(installer_id: str, req: WipeRequest):
        """Wipe the requested storage items.

        For any IRRECOVERABLE item, ``confirm_phrase`` must equal
        ``"wipe {installer_id}"`` (lowercase, exact) OR ``force`` must
        be true. The phrase check happens server-side — the frontend
        should also gate the UI, but this is the authoritative gate.
        """
        installer = get_installer(installer_id)
        if installer is None:
            raise HTTPException(404, f"Installer '{installer_id}' not found")

        # Determine whether any requested item is IRRECOVERABLE — only
        # then do we enforce the phrase check.
        from openjarvis.installers import Wipeability

        wanted = set(req.item_ids)
        irreplaceable_targeted = False
        for step in installer.steps:
            try:
                for it in step.storage_inventory():
                    if it.item_id in wanted and it.wipeability == Wipeability.IRRECOVERABLE:
                        irreplaceable_targeted = True
                        break
            except Exception:
                continue
            if irreplaceable_targeted:
                break

        expected_phrase = f"wipe {installer_id}"
        force = req.force
        if irreplaceable_targeted and not force:
            if req.confirm_phrase.strip().lower() != expected_phrase:
                raise HTTPException(
                    400,
                    detail={
                        "error": "confirm_phrase_required",
                        "expected": expected_phrase,
                        "message": (
                            "This wipe targets irrecoverable data. Type the "
                            f"phrase \"{expected_phrase}\" to confirm."
                        ),
                    },
                )
            # Phrase matched → treat as force-equivalent for the wipe call
            # (we've already done the human-in-the-loop check).
            force = True

        events: List[Dict[str, Any]] = []
        try:
            for progress in installer.wipe(
                req.item_ids, force=force, restart_after=req.restart_after
            ):
                events.append({
                    "step_idx": progress.step_idx,
                    "step_name": progress.step_name,
                    "percent": progress.percent,
                    "message": progress.message,
                })
        except WipeRefused as exc:
            raise HTTPException(400, str(exc))
        except InstallerError as exc:
            raise HTTPException(500, str(exc))
        finally:
            _invalidate_status_cache(installer_id)

        return {"installer_id": installer_id, "events": events, "ok": True}

    return router


def create_docker_router():
    """Return a small APIRouter for the global Docker resources page.

    Lives outside the per-installer router because Docker images are a
    *global* resource — they're shared across installers and not owned
    by any one of them.
    """
    try:
        from fastapi import APIRouter
    except ImportError as exc:
        raise ImportError("fastapi is required for the docker router") from exc

    router = APIRouter(prefix="/v1/docker", tags=["docker"])

    @router.get("/resources")
    async def resources():
        """List Docker images managed by OpenJarvis installers.

        Returns the image manifest joined with current ``docker images``
        output: which installers use each image, on-disk size, and
        whether the image is currently being used by a running container.
        """
        from openjarvis.installers.primitives import (
            docker_available,
            list_managed_images,
        )

        if not docker_available():
            return {
                "available": False,
                "images": [],
                "note": "Docker is not installed or not running on this machine.",
            }
        return {"available": True, "images": list_managed_images()}

    return router
