"""FastAPI router for ``/v1/tools`` — user-mutable tool settings.

Right now this only covers the web_search backend selector. The Tools
tab in the frontend reads the current config here and writes back when
the user picks a backend. Lives in its own router (rather than tacked
onto ``api_routes``) so the surface is easy to spot, version, and test.

Endpoints
---------
- ``GET  /v1/tools/web_search/config``  — current effective config
- ``PUT  /v1/tools/web_search/config``  — set/clear backend overrides
- ``DELETE /v1/tools/web_search/config`` — wipe the sidecar entirely

The PUT body is a partial: either or both of ``backend`` /
``searxng_url`` may be present. Passing an empty string for a field
clears the override and lets ``config.toml`` win again.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Pydantic schemas at module scope so FastAPI resolves them at startup.
try:
    from pydantic import BaseModel as _BaseModel
    from pydantic import Field as _Field

    class WebSearchConfigUpdate(_BaseModel):
        """Partial update for the web_search override sidecar.

        Both fields optional → "leave unchanged". Empty string → "clear
        the override". The validation of ``backend`` (recognised names,
        comma-separated chain) happens server-side inside
        ``save_web_search_settings`` — surfaces a 400 with the bad name.
        """

        backend: Optional[str] = _Field(
            default=None,
            description=(
                'Comma-separated chain like "searxng" or "tavily,duckduckgo".'
                ' Pass an empty string to clear the override.'
            ),
        )
        searxng_url: Optional[str] = _Field(
            default=None,
            description='Override URL for the SearXNG backend.'
            ' Pass an empty string to clear.',
        )

except ImportError:
    WebSearchConfigUpdate = None  # type: ignore[assignment,misc]


def _effective_config_payload() -> Dict[str, Any]:
    """Assemble the current effective config (sidecar + config.toml).

    Returns the shape the frontend expects:
    - ``backend``: the chain that will actually run, post-precedence
    - ``backend_source``: ``"env" | "sidecar" | "config" | "default"``
      so the UI can show "set by env var — UI changes have no effect"
    - ``searxng_url``: same shape for the URL
    - ``sidecar``: raw sidecar contents, or ``null`` (handy for debug)
    """
    import os

    from openjarvis.tools.web_search_settings import load_web_search_settings

    sidecar = load_web_search_settings()

    # Read the static config.toml view of these settings (might not be
    # set; fall back to defaults). We don't import ``load_config`` at
    # module top because it does hardware detection on first call.
    try:
        from openjarvis.core.config import load_config

        toml = load_config().tools.web_search
        toml_backend = toml.backend or "auto"
        toml_url = toml.searxng_url or "http://127.0.0.1:8888"
    except Exception:
        toml_backend = "auto"
        toml_url = "http://127.0.0.1:8888"

    env_backend = os.environ.get("OPENJARVIS_WEB_SEARCH_BACKEND")
    env_url = os.environ.get("OPENJARVIS_SEARXNG_URL")

    if env_backend:
        backend, backend_source = env_backend, "env"
    elif sidecar and sidecar.get("backend"):
        backend, backend_source = sidecar["backend"], "sidecar"
    elif toml_backend and toml_backend != "auto":
        backend, backend_source = toml_backend, "config"
    else:
        backend, backend_source = "auto", "default"

    if env_url:
        url, url_source = env_url, "env"
    elif sidecar and sidecar.get("searxng_url"):
        url, url_source = sidecar["searxng_url"], "sidecar"
    elif toml_url and toml_url != "http://127.0.0.1:8888":
        url, url_source = toml_url, "config"
    else:
        url, url_source = toml_url, "default"

    return {
        "backend": backend,
        "backend_source": backend_source,
        "searxng_url": url,
        "searxng_url_source": url_source,
        "sidecar": sidecar,
        # Convenience: list of recognised backends for the UI to render
        # as choices without having to hard-code the same list twice.
        "available_backends": ["auto", "tavily", "duckduckgo", "searxng"],
    }


def create_tools_router():
    """Return the APIRouter — factory pattern, like the other routers."""
    try:
        from fastapi import APIRouter, HTTPException
    except ImportError as exc:
        raise ImportError("fastapi is required for the tools router") from exc

    if WebSearchConfigUpdate is None:
        raise ImportError("pydantic is required for the tools router")

    router = APIRouter(prefix="/v1/tools", tags=["tools"])

    @router.get("/web_search/config")
    async def get_web_search_config():
        """Return the effective web_search config and the override source."""
        return _effective_config_payload()

    @router.put("/web_search/config")
    async def put_web_search_config(payload: WebSearchConfigUpdate):
        """Update the sidecar override. Both fields are optional.

        Pass ``backend=""`` or ``searxng_url=""`` to clear that field —
        the next layer in the precedence chain (config.toml) then wins.
        """
        from openjarvis.tools.web_search_settings import save_web_search_settings

        try:
            save_web_search_settings(
                backend=payload.backend,
                searxng_url=payload.searxng_url,
            )
        except ValueError as exc:
            # Unknown backend name — surface as 400 so the frontend can
            # show the exact reason ("tavili" is a typo, not a server bug).
            raise HTTPException(400, str(exc))
        return _effective_config_payload()

    @router.delete("/web_search/config")
    async def delete_web_search_config():
        """Wipe the sidecar so config.toml + defaults take over again."""
        from openjarvis.tools.web_search_settings import clear_web_search_settings

        clear_web_search_settings()
        return _effective_config_payload()

    return router


__all__ = ["create_tools_router"]
