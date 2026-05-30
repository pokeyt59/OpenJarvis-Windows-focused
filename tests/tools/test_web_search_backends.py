"""Tests for the backend-chain + SearXNG additions to WebSearchTool.

The pre-existing ``test_web_search.py`` covers the legacy Tavily → DDG
path and URL detection / SSRF handling. This file focuses on:

- ``_resolve_backend_chain`` — the string-to-list parser
- SearXNG backend HTTP call + SSRF check
- Backend chain dispatch — first success wins, failures roll forward
- Env var precedence — ``OPENJARVIS_WEB_SEARCH_BACKEND`` wins over
  the config-file default
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------


class TestResolveBackendChain:
    def test_auto_expands_to_default_pair(self) -> None:
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("auto") == ["tavily", "duckduckgo"]

    def test_empty_string_uses_default(self) -> None:
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("") == ["tavily", "duckduckgo"]

    def test_single_backend(self) -> None:
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("searxng") == ["searxng"]

    def test_multi_backend_preserves_order(self) -> None:
        """Order matters — first backend in the string is tried first."""
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("searxng,duckduckgo,tavily") == [
            "searxng",
            "duckduckgo",
            "tavily",
        ]

    def test_unknown_entries_dropped_silently(self) -> None:
        """Stale entries shouldn't break the chain."""
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("brave,searxng,bing") == ["searxng"]

    def test_whitespace_tolerant(self) -> None:
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain(" searxng , duckduckgo ") == [
            "searxng",
            "duckduckgo",
        ]

    def test_case_insensitive(self) -> None:
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("SearxNG,DuckDuckGo") == [
            "searxng",
            "duckduckgo",
        ]

    def test_only_unknown_falls_back_to_default(self) -> None:
        """If user typos everything, don't leave them with an empty chain."""
        from openjarvis.tools.web_search import _resolve_backend_chain

        assert _resolve_backend_chain("brave,bing") == ["tavily", "duckduckgo"]


# ---------------------------------------------------------------------------
# Env var precedence
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_env_var_wins_over_config_default(self, monkeypatch) -> None:
        """``OPENJARVIS_WEB_SEARCH_BACKEND`` overrides whatever load_config returns."""
        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")
        tool = WebSearchTool(api_key="ignored")
        assert tool._backends == ["searxng"]

    def test_explicit_arg_wins_when_env_unset(self, monkeypatch) -> None:
        """When env not set, the constructor arg is used."""
        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.delenv("OPENJARVIS_WEB_SEARCH_BACKEND", raising=False)
        tool = WebSearchTool(api_key="x", backend="duckduckgo")
        assert tool._backends == ["duckduckgo"]

    def test_env_wins_over_explicit_arg(self, monkeypatch) -> None:
        """Env var is the highest-precedence source — by design.

        Lets dev runs / recipe tests force a backend without rebuilding
        the agent.
        """
        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")
        tool = WebSearchTool(api_key="x", backend="duckduckgo")
        assert tool._backends == ["searxng"]

    def test_searxng_url_env_overrides_default(self, monkeypatch) -> None:
        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.setenv("OPENJARVIS_SEARXNG_URL", "http://other-host:9000")
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")
        tool = WebSearchTool(api_key="x")
        assert tool._searxng_url == "http://other-host:9000"

    def test_searxng_url_strips_trailing_slash(self, monkeypatch) -> None:
        """URL is joined as ``{url}/search`` — trailing slashes corrupt it."""
        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.setenv("OPENJARVIS_SEARXNG_URL", "http://127.0.0.1:8888/")
        tool = WebSearchTool(api_key="x")
        assert tool._searxng_url == "http://127.0.0.1:8888"


# ---------------------------------------------------------------------------
# SearXNG backend HTTP call
# ---------------------------------------------------------------------------


class TestSearXNGBackend:
    def _stub_ssrf(self, monkeypatch) -> None:
        """SSRF check requires the Rust extension — stub it for unit tests."""
        import openjarvis.tools.web_search as _ws

        monkeypatch.setattr(_ws, "check_ssrf", lambda url: None)

    def test_searxng_posts_to_search_endpoint(self, monkeypatch) -> None:
        """The request must hit ``{url}/search`` with ``q`` and ``format=json``.

        SearXNG returns HTML by default; the installer recipe enables
        the JSON output format in settings.yml. Requesting format=json
        is what makes the response shape predictable for us.
        """
        import httpx

        from openjarvis.tools.web_search import WebSearchTool

        self._stub_ssrf(monkeypatch)
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")
        monkeypatch.setenv("OPENJARVIS_SEARXNG_URL", "http://127.0.0.1:8888")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"title": "Hit 1", "url": "https://example.com/1", "content": "Snippet 1"},
                {"title": "Hit 2", "url": "https://example.com/2", "content": "Snippet 2"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post = MagicMock(return_value=mock_resp)
        monkeypatch.setattr(httpx, "post", mock_post)

        tool = WebSearchTool(api_key="ignored")
        result = tool.execute(query="who is winston churchill", max_results=2)

        assert result.success is True
        assert result.metadata["engine"] == "searxng"
        assert result.metadata["num_results"] == 2
        assert "Hit 1" in result.content
        assert "Hit 2" in result.content

        # Verify the call shape — endpoint, body, format
        call = mock_post.call_args
        assert call.args[0] == "http://127.0.0.1:8888/search"
        assert call.kwargs["data"]["q"] == "who is winston churchill"
        assert call.kwargs["data"]["format"] == "json"

    def test_searxng_truncates_to_max_results(self, monkeypatch) -> None:
        """When SearXNG returns more than max_results, the tool slices."""
        import httpx

        from openjarvis.tools.web_search import WebSearchTool

        self._stub_ssrf(monkeypatch)
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"title": f"Hit {i}", "url": f"https://x/{i}", "content": "c"}
                for i in range(10)
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(httpx, "post", MagicMock(return_value=mock_resp))

        tool = WebSearchTool(api_key="ignored", max_results=3)
        result = tool.execute(query="hello")
        assert result.metadata["num_results"] == 3
        assert "Hit 0" in result.content
        assert "Hit 2" in result.content
        assert "Hit 3" not in result.content  # truncated

    def test_searxng_ssrf_check_blocks_unsafe_url(self, monkeypatch) -> None:
        """If user sets a SearXNG URL pointing at private infra, refuse it."""
        import openjarvis.tools.web_search as _ws

        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")
        monkeypatch.setenv("OPENJARVIS_SEARXNG_URL", "http://10.0.0.5:8080")
        monkeypatch.setattr(
            _ws, "check_ssrf", lambda url: "Private RFC1918 range blocked"
        )

        tool = WebSearchTool(api_key="ignored")
        result = tool.execute(query="hello")
        # The SearXNG backend raises → backend chain has nothing else
        # to try (we forced only searxng), so the whole execute fails.
        assert result.success is False
        assert "backends failed" in result.content.lower()


# ---------------------------------------------------------------------------
# Backend chain dispatch — first success wins, failures roll forward
# ---------------------------------------------------------------------------


class TestBackendChainDispatch:
    def _stub_ssrf(self, monkeypatch) -> None:
        import openjarvis.tools.web_search as _ws

        monkeypatch.setattr(_ws, "check_ssrf", lambda url: None)

    def test_chain_falls_through_on_failure(self, monkeypatch) -> None:
        """First backend errors → second backend runs → success.

        Critical property for real-world reliability: SearXNG might be
        down (Docker container stopped) but DDG still works. The user
        shouldn't see a tool failure for that.
        """
        import httpx

        from openjarvis.tools.web_search import WebSearchTool

        self._stub_ssrf(monkeypatch)
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng,duckduckgo")

        # SearXNG raises — simulates container down
        monkeypatch.setattr(
            httpx, "post", MagicMock(side_effect=httpx.ConnectError("nope"))
        )

        # DDG returns mocked results
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = [
            {"title": "DDG", "href": "https://x", "body": "fine"}
        ]
        mock_ddgs_module = MagicMock()
        mock_ddgs_module.DDGS.return_value = mock_ddgs
        monkeypatch.setitem(sys.modules, "ddgs", mock_ddgs_module)

        tool = WebSearchTool(api_key="ignored")
        result = tool.execute(query="hello")
        assert result.success is True
        assert result.metadata["engine"] == "duckduckgo"
        # Both attempts recorded so users can debug what was tried
        assert result.metadata["attempted"] == "searxng,duckduckgo"

    def test_metadata_records_all_attempts(self, monkeypatch) -> None:
        """Even on success, ``attempted`` lists everything tried — useful
        for debugging surprising routing."""
        import httpx

        from openjarvis.tools.web_search import WebSearchTool

        self._stub_ssrf(monkeypatch)
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(httpx, "post", MagicMock(return_value=mock_resp))

        tool = WebSearchTool(api_key="ignored")
        result = tool.execute(query="hello")
        assert result.metadata["attempted"] == "searxng"

    def test_all_backends_failing_returns_error(self, monkeypatch) -> None:
        import httpx

        from openjarvis.tools.web_search import WebSearchTool

        self._stub_ssrf(monkeypatch)
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng,duckduckgo")
        monkeypatch.setattr(
            httpx, "post", MagicMock(side_effect=httpx.ConnectError("nope"))
        )

        # DDG also broken — make import fail
        monkeypatch.delitem(sys.modules, "ddgs", raising=False)

        import builtins

        original = builtins.__import__

        def _mock_import(name, *args, **kwargs):
            if name == "ddgs":
                raise ImportError("ddgs missing")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _mock_import)

        tool = WebSearchTool(api_key="ignored")
        result = tool.execute(query="hello")
        assert result.success is False
        assert "All web search backends failed" in result.content


# ---------------------------------------------------------------------------
# Backend chain metadata exposure
# ---------------------------------------------------------------------------


class TestSpecMetadata:
    def test_spec_lists_configured_backends(self, monkeypatch) -> None:
        """The tool spec advertises its chain so MCP introspection works."""
        from openjarvis.tools.web_search import WebSearchTool

        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "searxng,duckduckgo")
        tool = WebSearchTool(api_key="ignored")
        assert tool.spec.metadata["backends"] == "searxng,duckduckgo"
