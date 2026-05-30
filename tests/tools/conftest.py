"""Test isolation for ``tests/tools/``.

The web_search tool reads ``~/.openjarvis/tools/web_search.json`` at
construction time (see ``openjarvis.tools.web_search_settings``). If a
developer has toggled "Use SearXNG" in the Tools tab, that sidecar
overrides every test expectation — the tool's backend chain comes back
as ``["searxng"]`` regardless of constructor kwargs or stubbed
``ddgs`` / ``tavily`` modules. Errors then surface as "All web search
backends failed (searxng). Last error: No module named
'openjarvis_rust'" because the searxng path calls ``check_ssrf``.

Autouse so every test in this dir gets a clean slate without having to
remember to opt in. Pattern mirrors the local ``tmp_home`` fixture in
``test_web_search_settings.py:19-41`` — the two coexist cleanly because
both ultimately monkeypatch the same module attrs.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_web_search_sidecar(tmp_path, monkeypatch):
    """Redirect the web_search sidecar to a per-test tmp dir."""
    fake_tools_dir = tmp_path / ".openjarvis" / "tools"
    fake_sidecar = fake_tools_dir / "web_search.json"

    # The sidecar module computed these at import — patching ``Path.home``
    # is too late, so we replace the module-level constants directly.
    monkeypatch.setattr(
        "openjarvis.tools.web_search_settings._TOOLS_DIR", fake_tools_dir
    )
    monkeypatch.setattr(
        "openjarvis.tools.web_search_settings._SIDECAR_PATH", fake_sidecar
    )

    # Env vars beat the sidecar — clear them so a developer's shell
    # session doesn't leak in either.
    monkeypatch.delenv("OPENJARVIS_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("OPENJARVIS_SEARXNG_URL", raising=False)
