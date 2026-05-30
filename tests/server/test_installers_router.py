"""Tests for the /v1/installers + /v1/docker API routers.

The SearXNG installer is the only one currently registered, so most
endpoints are exercised against it. The Docker primitives (subprocess
calls) are mocked out so tests run without a real Docker installation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# App fixture — wires both routers like the real server does
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi not installed")

    from openjarvis.server.installers_router import (
        _invalidate_status_cache,
        create_docker_router,
        create_installers_router,
    )

    # Clear the per-process status cache between tests so cached results
    # from a previous test don't leak. The router module is a singleton.
    _invalidate_status_cache()

    _app = FastAPI()
    _app.include_router(create_installers_router())
    _app.include_router(create_docker_router())
    return TestClient(_app)


# ---------------------------------------------------------------------------
# Listing + status
# ---------------------------------------------------------------------------


class TestListAndStatus:
    def test_list_includes_searxng(self, app):
        """SearXNG recipe registers itself on import."""
        resp = app.get("/v1/installers")
        assert resp.status_code == 200
        data = resp.json()
        ids = [i["installer_id"] for i in data["installers"]]
        assert "web_search.searxng" in ids

    def test_status_shape(self, app):
        """/status returns display_name + status + per-step list."""
        resp = app.get("/v1/installers/web_search.searxng/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["installer_id"] == "web_search.searxng"
        assert "display_name" in body
        assert "status" in body
        assert "steps" in body
        assert len(body["steps"]) == 5  # DockerEnvCheck, Image, ConfigFile, Run, WaitForHTTP
        for step in body["steps"]:
            assert "name" in step
            assert "status" in step

    def test_status_404_for_unknown_installer(self, app):
        resp = app.get("/v1/installers/no_such_thing/status")
        assert resp.status_code == 404

    def test_status_cache_returns_same_payload_within_ttl(self, app):
        """Within the 30s TTL, repeated /status hits return identical payloads."""
        r1 = app.get("/v1/installers/web_search.searxng/status").json()
        r2 = app.get("/v1/installers/web_search.searxng/status").json()
        # Status content should match — both came from the cache or both
        # came from a fresh detect within the same second
        assert r1["status"] == r2["status"]

    def test_status_refresh_busts_cache(self, app):
        """POST /status/refresh should hit detect again."""
        # Prime cache
        app.get("/v1/installers/web_search.searxng/status")
        # Refresh should succeed and return a payload
        resp = app.post("/v1/installers/web_search.searxng/status/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert body["installer_id"] == "web_search.searxng"

    def test_refresh_unknown_404(self, app):
        resp = app.post("/v1/installers/nope/status/refresh")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /run SSE stream
# ---------------------------------------------------------------------------


class TestRunStream:
    def test_run_404_for_unknown(self, app):
        resp = app.post("/v1/installers/no_such/run")
        assert resp.status_code == 404

    def test_run_emits_progress_events(self, app):
        """Each step yields one or more Progress events as SSE ``data:`` lines.

        We mock ``installer.run`` directly to keep the test from poking
        real Docker. The router should re-serialise each Progress dict
        as a JSON ``data:`` line and finish with ``event: done``.
        """
        from openjarvis.installers.base import Progress, ProgressLevel
        from openjarvis.installers.registry import get_installer

        installer = get_installer("web_search.searxng")
        assert installer is not None

        fake_events = [
            Progress(step_idx=0, step_name="docker", percent=100, message="docker ok"),
            Progress(step_idx=1, step_name="image", percent=50, message="pulling…"),
            Progress(step_idx=1, step_name="image", percent=100, message="image done"),
        ]
        with patch.object(installer, "run", return_value=iter(fake_events)):
            with app.stream("POST", "/v1/installers/web_search.searxng/run") as resp:
                assert resp.status_code == 200
                body = b"".join(resp.iter_bytes()).decode()

        # Three data: lines + one final event: done
        assert body.count("data: ") == 4  # 3 progress + 1 done
        assert "event: done" in body
        assert "docker ok" in body
        assert "pulling" in body or "pulling…" in body

    def test_run_emits_error_event_on_installer_error(self, app):
        """A raised ``InstallerError`` becomes an ``event: error`` line, not 500."""
        from openjarvis.installers.base import InstallerError, Progress
        from openjarvis.installers.registry import get_installer

        installer = get_installer("web_search.searxng")
        assert installer is not None

        def _gen():
            yield Progress(step_idx=0, step_name="docker", percent=50, message="checking")
            raise InstallerError("Docker not running")

        with patch.object(installer, "run", return_value=_gen()):
            with app.stream("POST", "/v1/installers/web_search.searxng/run") as resp:
                body = b"".join(resp.iter_bytes()).decode()

        assert "event: error" in body
        assert "Docker not running" in body

    def test_run_forwards_link_field_on_installer_error(self, app):
        """A structured ``link`` on InstallerError is forwarded to the SSE payload.

        The Docker primitive uses this: when Docker Desktop is missing it
        raises ``InstallerError("...", link={"label": "Install Docker
        Desktop", "url": "https://..."})`` so the UI can render a one-click
        recovery button instead of asking the user to copy-paste a URL.
        """
        import json

        from openjarvis.installers.base import InstallerError, Progress
        from openjarvis.installers.registry import get_installer

        installer = get_installer("web_search.searxng")
        assert installer is not None

        # ``_gen`` must contain at least one ``yield`` so calling it
        # returns a generator object (otherwise the raise fires
        # immediately when we set up the patch, before the route runs).
        def _gen():
            yield Progress(step_idx=0, step_name="docker", percent=0, message="checking")
            raise InstallerError(
                "Docker is not available on this machine.",
                link={"label": "Install Docker Desktop", "url": "https://example.test/dl"},
            )

        with patch.object(installer, "run", return_value=_gen()):
            with app.stream("POST", "/v1/installers/web_search.searxng/run") as resp:
                body = b"".join(resp.iter_bytes()).decode()

        # The error event should carry both ``error`` and ``link`` keys.
        assert "event: error" in body
        # Pluck the JSON payload from the data: line so we don't rely on
        # the exact field order in the rendered string.
        data_line = next(
            ln for ln in body.splitlines() if ln.startswith("data:") and "link" in ln
        )
        payload = json.loads(data_line[len("data:") :].strip())
        assert payload["error"] == "Docker is not available on this machine."
        assert payload["link"] == {
            "label": "Install Docker Desktop",
            "url": "https://example.test/dl",
        }


# ---------------------------------------------------------------------------
# Storage inventory + wipe (with confirm_phrase)
# ---------------------------------------------------------------------------


class TestStorageAndWipe:
    def test_storage_endpoint_returns_report_shape(self, app):
        """/storage returns total_bytes, by_kind, and an item list."""
        resp = app.get("/v1/installers/web_search.searxng/storage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["installer_id"] == "web_search.searxng"
        assert "total_bytes" in body
        assert "by_kind" in body
        assert "items" in body
        # Each item carries the wipeability tier so the frontend knows
        # whether to demand a confirm_phrase
        for it in body["items"]:
            assert "item_id" in it
            assert "wipeability" in it
            assert it["wipeability"] in ("ephemeral", "replaceable", "irrecoverable")

    def test_wipe_requires_confirm_phrase_for_irrecoverable(self, app):
        """Wipe with an IRRECOVERABLE item id must demand the phrase.

        Server-side enforcement — even if the frontend forgets to gate
        the UI, the backend refuses without the literal phrase.
        """
        from openjarvis.installers.base import StorageItem, Wipeability
        from openjarvis.installers.registry import get_installer

        installer = get_installer("web_search.searxng")
        assert installer is not None

        # Inject a synthetic IRRECOVERABLE inventory item by mocking
        # storage_inventory on the first step.
        fake_item = StorageItem(
            item_id="fake-irrecov",
            kind=Wipeability.IRRECOVERABLE,  # any StorageKind would do
            description="fake",
            size_bytes=100,
            wipeability=Wipeability.IRRECOVERABLE,
            path=None,
        )
        with patch.object(
            installer.steps[0], "storage_inventory", return_value=[fake_item]
        ):
            # No phrase → 400 with the expected phrase shown
            resp = app.post(
                "/v1/installers/web_search.searxng/wipe",
                json={"item_ids": ["fake-irrecov"], "confirm_phrase": ""},
            )
            assert resp.status_code == 400
            err = resp.json()["detail"]
            assert err["error"] == "confirm_phrase_required"
            assert err["expected"] == "wipe web_search.searxng"

    def test_wipe_proceeds_when_phrase_matches(self, app):
        """With the correct phrase, wipe runs and returns events."""
        from openjarvis.installers.base import StorageItem, Wipeability
        from openjarvis.installers.registry import get_installer

        installer = get_installer("web_search.searxng")
        assert installer is not None

        fake_item = StorageItem(
            item_id="fake-irrecov",
            kind=Wipeability.IRRECOVERABLE,
            description="fake",
            size_bytes=100,
            wipeability=Wipeability.IRRECOVERABLE,
            path=None,
        )
        # Mock storage_inventory on every step, plus the actual wipe to
        # avoid touching disk
        with patch.object(
            installer.steps[0], "storage_inventory", return_value=[fake_item]
        ), patch.object(
            installer, "wipe", return_value=iter([])
        ) as mock_wipe:
            resp = app.post(
                "/v1/installers/web_search.searxng/wipe",
                json={
                    "item_ids": ["fake-irrecov"],
                    "confirm_phrase": "wipe web_search.searxng",
                },
            )
        assert resp.status_code == 200
        # The wipe was called with force=True (phrase matched → bypass
        # the inner refusal check)
        mock_wipe.assert_called_once()
        kwargs = mock_wipe.call_args.kwargs
        assert kwargs["force"] is True

    def test_wipe_replaceable_doesnt_require_phrase(self, app):
        """REPLACEABLE items can be wiped without the ceremony."""
        from openjarvis.installers.base import (
            StorageItem,
            StorageKind,
            Wipeability,
        )
        from openjarvis.installers.registry import get_installer

        installer = get_installer("web_search.searxng")
        assert installer is not None

        fake_item = StorageItem(
            item_id="settings",
            kind=StorageKind.CONFIG,
            description="config",
            size_bytes=4096,
            wipeability=Wipeability.REPLACEABLE,
            path=None,
        )
        with patch.object(
            installer.steps[0], "storage_inventory", return_value=[fake_item]
        ), patch.object(installer, "wipe", return_value=iter([])):
            resp = app.post(
                "/v1/installers/web_search.searxng/wipe",
                json={"item_ids": ["settings"], "confirm_phrase": ""},
            )
        assert resp.status_code == 200

    def test_wipe_404_for_unknown_installer(self, app):
        resp = app.post(
            "/v1/installers/no_such/wipe",
            json={"item_ids": ["x"], "confirm_phrase": ""},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Docker resources endpoint
# ---------------------------------------------------------------------------


class TestDockerResources:
    """The router imports ``docker_available`` and ``list_managed_images``
    from ``openjarvis.installers.primitives`` (the package __init__
    re-exports them). Patching at the source module would leave the
    re-exported binding in the package unchanged, so we patch the
    package-level name — that's what the route actually looks up.
    """

    def test_resources_reports_unavailable_when_docker_missing(self, app):
        """When Docker isn't installed, return ``available=false``, not 500."""
        with patch(
            "openjarvis.installers.primitives.docker_available",
            return_value=False,
        ):
            resp = app.get("/v1/docker/resources")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["images"] == []

    def test_resources_returns_managed_image_list(self, app):
        """With Docker available, returns the manifest's image list."""
        with patch(
            "openjarvis.installers.primitives.docker_available",
            return_value=True,
        ), patch(
            "openjarvis.installers.primitives.list_managed_images",
            return_value=[
                {
                    "image_ref": "searxng/searxng:latest",
                    "size_bytes": 123_456_789,
                    "installer_ids": ["web_search.searxng"],
                    "in_use": True,
                    "image_exists": True,
                }
            ],
        ):
            resp = app.get("/v1/docker/resources")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert len(body["images"]) == 1
        assert body["images"][0]["image_ref"] == "searxng/searxng:latest"
