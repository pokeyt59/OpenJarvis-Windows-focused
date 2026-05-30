"""Tests for the web_search sidecar settings layer.

Covers:
- Round-trip save/load of the JSON sidecar
- Precedence (env > sidecar > toml > default) for both backend + URL
- Validation: unknown backend names raise ValueError
- Empty-string fields clear the override and remove the file when nothing remains
- WebSearchTool actually consults the sidecar at construction time
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirect the sidecar location into a tmp dir for each test.

    We patch ``DEFAULT_CONFIG_DIR`` *and* the module-level ``_TOOLS_DIR``
    / ``_SIDECAR_PATH`` constants the sidecar module bound at import.
    Patching just ``Path.home`` doesn't work because those constants
    were already computed.
    """
    fake_dir = tmp_path / ".openjarvis"
    fake_tools_dir = fake_dir / "tools"
    fake_sidecar = fake_tools_dir / "web_search.json"

    monkeypatch.setattr(
        "openjarvis.tools.web_search_settings._TOOLS_DIR", fake_tools_dir
    )
    monkeypatch.setattr(
        "openjarvis.tools.web_search_settings._SIDECAR_PATH", fake_sidecar
    )
    # Clear any env vars that would override the sidecar in tests.
    monkeypatch.delenv("OPENJARVIS_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("OPENJARVIS_SEARXNG_URL", raising=False)
    return fake_sidecar


# ---------------------------------------------------------------------------
# Round-trip + validation
# ---------------------------------------------------------------------------


class TestSidecarRoundtrip:
    def test_load_returns_none_when_no_file(self, tmp_home):
        from openjarvis.tools.web_search_settings import load_web_search_settings

        assert load_web_search_settings() is None

    def test_save_then_load_returns_same_backend(self, tmp_home):
        from openjarvis.tools.web_search_settings import (
            load_web_search_settings,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng")
        data = load_web_search_settings()
        assert data is not None
        assert data["backend"] == "searxng"
        assert "updated_at" in data  # always stamped

    def test_save_normalises_chain(self, tmp_home):
        """Whitespace, casing, and dupes are cleaned up before write."""
        from openjarvis.tools.web_search_settings import (
            load_web_search_settings,
            save_web_search_settings,
        )

        save_web_search_settings(backend="  Tavily , DUCKDUCKGO , tavily ")
        data = load_web_search_settings()
        # Order preserved, lowercase, deduped
        assert data["backend"] == "tavily,duckduckgo"

    def test_save_unknown_backend_raises(self, tmp_home):
        from openjarvis.tools.web_search_settings import save_web_search_settings

        with pytest.raises(ValueError):
            save_web_search_settings(backend="bogus_engine")

    def test_save_partial_update_preserves_other_field(self, tmp_home):
        """Setting backend then setting url shouldn't lose the backend."""
        from openjarvis.tools.web_search_settings import (
            load_web_search_settings,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng")
        save_web_search_settings(searxng_url="http://localhost:9000")
        data = load_web_search_settings()
        assert data["backend"] == "searxng"
        assert data["searxng_url"] == "http://localhost:9000"

    def test_empty_string_clears_field(self, tmp_home):
        """Empty string is the documented signal to remove an override."""
        from openjarvis.tools.web_search_settings import (
            load_web_search_settings,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng", searxng_url="http://x")
        save_web_search_settings(backend="")
        data = load_web_search_settings()
        # backend cleared; URL still present
        assert data is not None
        assert "backend" not in data
        assert data["searxng_url"] == "http://x"

    def test_clearing_all_removes_file(self, tmp_home):
        """When nothing remains, the file is deleted entirely."""
        from openjarvis.tools.web_search_settings import (
            load_web_search_settings,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng")
        save_web_search_settings(backend="", searxng_url="")
        # Sidecar gone — next load is None, not a stub with just updated_at
        assert load_web_search_settings() is None
        assert not tmp_home.exists()

    def test_clear_helper_unlinks_file(self, tmp_home):
        from openjarvis.tools.web_search_settings import (
            clear_web_search_settings,
            load_web_search_settings,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng")
        assert tmp_home.exists()
        clear_web_search_settings()
        assert not tmp_home.exists()
        assert load_web_search_settings() is None


# ---------------------------------------------------------------------------
# Precedence helpers
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_env_beats_sidecar(self, tmp_home):
        from openjarvis.tools.web_search_settings import (
            get_effective_backend,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng")
        # env_value passed explicitly to keep the test deterministic
        assert (
            get_effective_backend(env_value="tavily", toml_value="auto") == "tavily"
        )

    def test_sidecar_beats_toml(self, tmp_home):
        from openjarvis.tools.web_search_settings import (
            get_effective_backend,
            save_web_search_settings,
        )

        save_web_search_settings(backend="searxng")
        assert (
            get_effective_backend(env_value=None, toml_value="tavily") == "searxng"
        )

    def test_toml_wins_when_no_sidecar(self, tmp_home):
        from openjarvis.tools.web_search_settings import get_effective_backend

        assert (
            get_effective_backend(env_value=None, toml_value="duckduckgo")
            == "duckduckgo"
        )

    def test_default_auto_when_nothing_set(self, tmp_home):
        from openjarvis.tools.web_search_settings import get_effective_backend

        assert get_effective_backend(env_value=None, toml_value="") == "auto"

    def test_searxng_url_precedence(self, tmp_home):
        from openjarvis.tools.web_search_settings import (
            get_effective_searxng_url,
            save_web_search_settings,
        )

        save_web_search_settings(searxng_url="http://sidecar:8888")
        # env wins
        assert (
            get_effective_searxng_url(
                env_value="http://env:9999", toml_value="http://toml:7777"
            )
            == "http://env:9999"
        )
        # no env → sidecar wins
        assert (
            get_effective_searxng_url(
                env_value=None, toml_value="http://toml:7777"
            )
            == "http://sidecar:8888"
        )


# ---------------------------------------------------------------------------
# WebSearchTool consults the sidecar at construction
# ---------------------------------------------------------------------------


class TestToolHonoursSidecar:
    def test_tool_picks_up_sidecar_backend(self, tmp_home, monkeypatch):
        """Constructing WebSearchTool should pick up the sidecar choice."""
        from openjarvis.tools.web_search import WebSearchTool
        from openjarvis.tools.web_search_settings import save_web_search_settings

        save_web_search_settings(backend="searxng")
        tool = WebSearchTool()
        assert tool._backends == ["searxng"]

    def test_env_overrides_sidecar_in_tool(self, tmp_home, monkeypatch):
        from openjarvis.tools.web_search import WebSearchTool
        from openjarvis.tools.web_search_settings import save_web_search_settings

        save_web_search_settings(backend="searxng")
        monkeypatch.setenv("OPENJARVIS_WEB_SEARCH_BACKEND", "tavily,duckduckgo")
        tool = WebSearchTool()
        assert tool._backends == ["tavily", "duckduckgo"]

    def test_searxng_url_sidecar_applied(self, tmp_home, monkeypatch):
        from openjarvis.tools.web_search import WebSearchTool
        from openjarvis.tools.web_search_settings import save_web_search_settings

        save_web_search_settings(
            backend="searxng", searxng_url="http://192.168.1.42:8080"
        )
        tool = WebSearchTool()
        assert tool._searxng_url == "http://192.168.1.42:8080"


# ---------------------------------------------------------------------------
# Tools router HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_home):
    """FastAPI client over just the tools router."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi not installed")

    from openjarvis.server.tools_router import create_tools_router

    _app = FastAPI()
    _app.include_router(create_tools_router())
    return TestClient(_app)


class TestToolsRouter:
    def test_get_default_payload_shape(self, app):
        resp = app.get("/v1/tools/web_search/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend_source"] == "default"  # no overrides set
        assert "available_backends" in body
        assert "searxng" in body["available_backends"]

    def test_put_writes_sidecar_and_reflects_source(self, app):
        resp = app.put(
            "/v1/tools/web_search/config", json={"backend": "searxng"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend"] == "searxng"
        assert body["backend_source"] == "sidecar"

        # GET sees the same state on next read
        get_resp = app.get("/v1/tools/web_search/config")
        assert get_resp.json()["backend"] == "searxng"

    def test_put_unknown_backend_returns_400(self, app):
        resp = app.put(
            "/v1/tools/web_search/config", json={"backend": "tavili"}
        )
        assert resp.status_code == 400
        assert "tavili" in resp.json()["detail"]

    def test_put_empty_string_clears_override(self, app):
        app.put("/v1/tools/web_search/config", json={"backend": "searxng"})
        resp = app.put(
            "/v1/tools/web_search/config", json={"backend": ""}
        )
        assert resp.status_code == 200
        body = resp.json()
        # backend dropped from sidecar → source falls through to default
        assert body["backend_source"] in ("default", "config")

    def test_delete_clears_sidecar(self, app):
        app.put("/v1/tools/web_search/config", json={"backend": "searxng"})
        resp = app.delete("/v1/tools/web_search/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend_source"] in ("default", "config")
        assert body["sidecar"] is None
