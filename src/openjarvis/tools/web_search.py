"""Web search tool with pluggable backends.

Each backend is a tiny self-contained function ``(query, max_results)
→ formatted_text``. The tool resolves an *ordered fallback chain* at
construction time and tries them in turn — first success wins.

Configuration precedence (highest wins):
  1. Per-call ``params['max_results']``
  2. Env vars ``OPENJARVIS_WEB_SEARCH_BACKEND`` and
     ``OPENJARVIS_SEARXNG_URL`` — useful for dev runs / recipe tests
  3. ``[tools.web_search]`` section in ``~/.openjarvis/config.toml``
  4. Built-in defaults (``backend = "auto"``, which expands to
     ``"tavily,duckduckgo"`` for backward compat with installs that
     pre-date this refactor)

Why a fallback chain rather than a single backend: search is flaky on
real networks, and the user already pays for the chain (Tavily quota,
DDG rate limits, local SearXNG availability). A miss on one backend
shouldn't block the agent's tool call.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, List, Optional, Tuple

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.security.ssrf import check_ssrf
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

# "auto" expands to this chain — keeps behaviour identical to the
# pre-refactor implementation for users who haven't opted into SearXNG.
_AUTO_CHAIN: Tuple[str, ...] = ("tavily", "duckduckgo")

# Every recognised backend name → human-readable label (used in metadata).
# Adding a new backend means: add a name here + add a method on
# WebSearchTool + add it to ``_BACKEND_DISPATCH`` inside ``execute``.
_KNOWN_BACKENDS: Tuple[str, ...] = ("tavily", "duckduckgo", "searxng")


def _resolve_backend_chain(configured: str) -> List[str]:
    """Turn a comma-separated backend string into a clean ordered list.

    Unknown entries are silently dropped (with a debug log) rather than
    raising — the tool shouldn't break an agent run because a stale
    config mentions a backend we removed.
    """
    if not configured or configured.strip() == "auto":
        return list(_AUTO_CHAIN)
    chain: List[str] = []
    for raw in configured.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in _KNOWN_BACKENDS:
            logger.debug("Unknown web_search backend %r — skipping", name)
            continue
        chain.append(name)
    return chain or list(_AUTO_CHAIN)


@ToolRegistry.register("web_search")
class WebSearchTool(BaseTool):
    """Search the web via a configurable backend chain."""

    tool_id = "web_search"
    is_local = False

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        backend: str | None = None,
        searxng_url: str | None = None,
    ):
        # Tavily key resolution kept identical to the old behaviour for
        # backward compat — explicit arg > env var.
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY")
        self._max_results = max_results

        # Backend chain resolution: env var beats sidecar beats config
        # beats default. Reading config here (rather than at module
        # import) means tests can monkey-patch env vars and re-construct
        # the tool to pick them up.
        #
        # The sidecar layer (``web_search_settings``) is what the Tools
        # tab in the frontend writes when the user clicks "Use SearXNG"
        # — it's a JSON override at ~/.openjarvis/tools/web_search.json
        # that we consult between the env var and config.toml so a UI
        # toggle doesn't require hand-editing TOML.
        env_backend = os.environ.get("OPENJARVIS_WEB_SEARCH_BACKEND")
        config_backend = backend
        if config_backend is None:
            try:
                from openjarvis.core.config import load_config

                config_backend = load_config().tools.web_search.backend
            except Exception:
                # Config unavailable (early import, broken TOML) →
                # fall through to defaults rather than blowing up.
                config_backend = "auto"
        try:
            from openjarvis.tools.web_search_settings import get_effective_backend

            effective_backend = get_effective_backend(
                env_value=env_backend, toml_value=config_backend or "auto"
            )
        except Exception:
            # Sidecar module shouldn't ever break — but if it does, fall
            # back to the env-then-config logic we had before.
            effective_backend = env_backend or config_backend or "auto"
        self._backends: List[str] = _resolve_backend_chain(effective_backend)

        # SearXNG URL — same precedence shape (env > sidecar > config).
        env_url = os.environ.get("OPENJARVIS_SEARXNG_URL")
        if searxng_url is None:
            try:
                from openjarvis.core.config import load_config

                searxng_url = load_config().tools.web_search.searxng_url
            except Exception:
                searxng_url = "http://127.0.0.1:8888"
        try:
            from openjarvis.tools.web_search_settings import (
                get_effective_searxng_url,
            )

            effective_url = get_effective_searxng_url(
                env_value=env_url, toml_value=searxng_url or "http://127.0.0.1:8888"
            )
        except Exception:
            effective_url = env_url or searxng_url or "http://127.0.0.1:8888"
        self._searxng_url = effective_url.rstrip("/")

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_search",
            description=(
                "Search the web for current information."
                " Returns relevant search results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return.",
                    },
                },
                "required": ["query"],
            },
            category="search",
            metadata={
                "requires_api_key": "TAVILY_API_KEY",
                "backends": ",".join(self._backends),
            },
        )

    # ------------------------------------------------------------------
    # URL fetch fallback (when query contains a URL, just fetch it)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_url(text: str) -> bool:
        """Check if text is a URL."""
        stripped = text.strip()
        return stripped.startswith("http://") or stripped.startswith("https://")

    @staticmethod
    def _extract_url(text: str) -> str | None:
        """Extract the first URL from text, if any."""
        import re as _re

        match = _re.search(r"https?://[^\s,;\"'<>]+", text)
        return match.group(0).rstrip(".,;)") if match else None

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Convert known PDF URLs to their HTML equivalents."""
        import re as _re

        # arxiv: /pdf/ID → /abs/ID (abstract page with full metadata)
        m = _re.match(r"(https?://arxiv\.org)/pdf/(.+?)(?:\.pdf)?$", url)
        if m:
            return f"{m.group(1)}/abs/{m.group(2)}"
        return url

    @staticmethod
    def _fetch_url(url: str, max_chars: int = 6000) -> str:
        """Fetch a URL and return extracted text content."""
        import re as _re

        import httpx

        url = WebSearchTool._normalize_url(url)
        ssrf_error = check_ssrf(url)
        if ssrf_error:
            raise ValueError(ssrf_error)
        resp = httpx.get(
            url.strip(),
            follow_redirects=True,
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; OpenJarvis/1.0; +https://github.com/openjarvis)"
            },
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "application/pdf" in content_type:
            return (
                "[This URL points to a PDF file which"
                f" cannot be read directly. URL: {url}]"
            )
        html = resp.text
        # Strip script/style tags and their contents
        html = _re.sub(
            r"<(script|style)[^>]*>.*?</\1>",
            "",
            html,
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        # Strip HTML tags
        text = _re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = _re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Content truncated]"
        return text

    # ------------------------------------------------------------------
    # Backend implementations
    #
    # Each returns ``(formatted_text, num_results)``. They raise on
    # failure; the dispatch loop in ``execute`` catches and moves to
    # the next backend.
    # ------------------------------------------------------------------

    def _tavily_search(self, query: str, max_results: int) -> Tuple[str, int]:
        """Tavily REST search. Requires ``TAVILY_API_KEY``."""
        from tavily import TavilyClient

        if not self._api_key:
            raise RuntimeError("TAVILY_API_KEY not set")
        client = TavilyClient(api_key=self._api_key)
        response = client.search(query, max_results=max_results)
        results = response.get("results", [])
        formatted = "\n\n".join(
            f"**{r.get('title', 'Untitled')}**\n"
            f"{r.get('url', '')}\n{r.get('content', '')}"
            for r in results
        )
        return formatted, len(results)

    def _duckduckgo_search(self, query: str, max_results: int) -> Tuple[str, int]:
        """DuckDuckGo via the ``ddgs`` package — no auth needed."""
        from ddgs import DDGS

        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=max_results))
        formatted = "\n\n".join(
            f"**{r.get('title', 'Untitled')}**\n"
            f"{r.get('href', '')}\n{r.get('body', '')}"
            for r in results
        )
        return formatted, len(results)

    def _searxng_search(self, query: str, max_results: int) -> Tuple[str, int]:
        """Local SearXNG instance — opt-in, installed via the installer recipe.

        Hits ``{searxng_url}/search`` with ``format=json``. The
        installer recipe enables the JSON output format in its
        ``settings.yml`` (default SearXNG ships with HTML-only).

        SearXNG aggregates many upstream search engines, so we ask for
        a single composite call rather than per-engine. ``q`` is the
        query, ``format`` is the response shape.
        """
        import httpx

        url = f"{self._searxng_url}/search"
        # SSRF check: the user configured this URL, but defaults are
        # local loopback. If they set it to something pointing at
        # internal infra, check_ssrf will catch it.
        ssrf_error = check_ssrf(url)
        if ssrf_error:
            raise ValueError(ssrf_error)
        resp = httpx.post(
            url,
            data={"q": query, "format": "json"},
            timeout=20.0,
            headers={
                "User-Agent": "OpenJarvis/1.0 (web_search)",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        results = (body.get("results") or [])[:max_results]
        formatted = "\n\n".join(
            f"**{r.get('title', 'Untitled')}**\n"
            f"{r.get('url', '')}\n{r.get('content', '')}"
            for r in results
        )
        return formatted, len(results)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def execute(self, **params: Any) -> ToolResult:
        query = params.get("query", "")
        if not query:
            return ToolResult(
                tool_name="web_search",
                content="No query provided.",
                success=False,
            )

        # If the query contains a URL, fetch it directly instead of
        # searching. This shortcut predates the backend chain and stays
        # in place because it's almost always what the agent wants.
        url = self._extract_url(query) if not self._is_url(query) else query.strip()
        if url:
            try:
                content = self._fetch_url(url)
                return ToolResult(
                    tool_name="web_search",
                    content=content or "No content found at URL.",
                    success=True,
                    metadata={"url": url, "mode": "fetch"},
                )
            except Exception as exc:
                return ToolResult(
                    tool_name="web_search",
                    content=f"Failed to fetch URL: {exc}",
                    success=False,
                )

        max_results = params.get("max_results", self._max_results)

        # Resolve backend name → method. Methods are bound at call time
        # so subclasses can override individual backends.
        dispatch: dict[str, Callable[[str, int], Tuple[str, int]]] = {
            "tavily": self._tavily_search,
            "duckduckgo": self._duckduckgo_search,
            "searxng": self._searxng_search,
        }

        last_exc: Optional[Exception] = None
        attempted: List[str] = []
        for backend in self._backends:
            fn = dispatch.get(backend)
            if fn is None:
                continue
            attempted.append(backend)
            try:
                formatted, num = fn(query, max_results)
                return ToolResult(
                    tool_name="web_search",
                    content=formatted or "No results found.",
                    success=True,
                    metadata={
                        "engine": backend,
                        "num_results": num,
                        "attempted": ",".join(attempted),
                    },
                )
            except ImportError as exc:
                # Backend package not installed — log + try next without
                # surfacing this to the user (it's an install problem,
                # not a query problem).
                logger.debug(
                    "Backend %s missing dependency (%s) — trying next",
                    backend,
                    exc,
                )
                last_exc = exc
            except Exception as exc:
                logger.debug(
                    "Backend %s failed (%s) — trying next",
                    backend,
                    type(exc).__name__,
                )
                last_exc = exc

        # Every backend in the chain failed.
        if last_exc is None:
            msg = (
                "No web search backend configured. Set tools.web_search.backend "
                "in config.toml, or set OPENJARVIS_WEB_SEARCH_BACKEND."
            )
        else:
            msg = (
                f"All web search backends failed ({','.join(attempted)}). "
                f"Last error: {last_exc}"
            )
        return ToolResult(
            tool_name="web_search",
            content=msg,
            success=False,
            metadata={"attempted": ",".join(attempted)},
        )


__all__ = ["WebSearchTool"]
