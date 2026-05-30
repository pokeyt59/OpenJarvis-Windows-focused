"""Tests for the installer registry + the SearXNG recipe wiring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from openjarvis.installers.base import Installer, Wipeability
from openjarvis.installers.primitives import (
    ConfigFile,
    DockerEnvCheck,
    DockerImage,
    DockerRun,
    WaitForHTTP,
)
from openjarvis.installers.registry import (
    _clear_registry_for_tests,
    get_installer,
    list_installers,
    register_installer,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_get(self) -> None:
        inst = Installer(installer_id="x.y", display_name="X")
        register_installer(inst)
        # get_installer triggers _ensure_installers_loaded which may try to
        # import searxng — patch it out to keep this test isolated.
        with patch(
            "openjarvis.installers.registry._ensure_installers_loaded"
        ):
            got = get_installer("x.y")
        assert got is inst

    def test_get_unknown_returns_none(self) -> None:
        with patch(
            "openjarvis.installers.registry._ensure_installers_loaded"
        ):
            assert get_installer("does.not.exist") is None

    def test_register_replaces_existing(self) -> None:
        a = Installer(installer_id="x", display_name="A")
        b = Installer(installer_id="x", display_name="B")
        register_installer(a)
        register_installer(b)
        with patch(
            "openjarvis.installers.registry._ensure_installers_loaded"
        ):
            got = get_installer("x")
        assert got is b

    def test_list_installers_sorted(self) -> None:
        register_installer(Installer(installer_id="b", display_name="B"))
        register_installer(Installer(installer_id="a", display_name="A"))
        with patch(
            "openjarvis.installers.registry._ensure_installers_loaded"
        ):
            assert list_installers() == ["a", "b"]


# ---------------------------------------------------------------------------
# SearXNG recipe shape
# ---------------------------------------------------------------------------


class TestSearXNGRecipe:
    def test_recipe_imports_and_registers(self) -> None:
        # Importing the module triggers register_installer on first import;
        # if the module is already cached (e.g. another test file imported it),
        # the module body won't re-run after _clear_registry_for_tests, so
        # register explicitly here using the still-accessible module attribute.
        from openjarvis.installers.recipes import searxng

        register_installer(searxng.SEARXNG_INSTALLER)

        with patch(
            "openjarvis.installers.registry._ensure_installers_loaded"
        ):
            got = get_installer(searxng.INSTALLER_ID)
        assert got is searxng.SEARXNG_INSTALLER

    def test_step_order_is_correct(self) -> None:
        from openjarvis.installers.recipes.searxng import SEARXNG_INSTALLER

        types = [type(s) for s in SEARXNG_INSTALLER.steps]
        # env check, image pull, config file, container run, http wait — in that order
        assert types == [
            DockerEnvCheck,
            DockerImage,
            ConfigFile,
            DockerRun,
            WaitForHTTP,
        ]

    def test_image_step_is_bound_to_installer(self) -> None:
        from openjarvis.installers.recipes.searxng import (
            INSTALLER_ID,
            SEARXNG_INSTALLER,
        )

        image_step = [
            s for s in SEARXNG_INSTALLER.steps if isinstance(s, DockerImage)
        ][0]
        assert image_step._installer_id == INSTALLER_ID

    def test_settings_yaml_enables_json_output(self) -> None:
        from openjarvis.installers.recipes.searxng import _settings_yml

        text = _settings_yml()
        # The critical bit — JSON must be in search.formats.
        assert "formats:" in text
        assert "- json" in text
        # And the rate limiter must be off for local agent use.
        assert "limiter: false" in text

    def test_settings_yaml_has_per_user_secret_key(self) -> None:
        from openjarvis.installers.recipes.searxng import _settings_yml

        text = _settings_yml()
        assert "secret_key:" in text
        # Should not be a literal placeholder.
        assert "secret_key: \"\"" not in text
        assert "secret_key: 'CHANGE_ME'" not in text

    def test_docker_run_volume_delegates_storage_to_configfile(self) -> None:
        from openjarvis.installers.recipes.searxng import SEARXNG_INSTALLER

        run_step = [
            s for s in SEARXNG_INSTALLER.steps if isinstance(s, DockerRun)
        ][0]
        # The volume should set report_storage=False so the Storage panel
        # doesn't show settings.yml twice.
        for vol in run_step.volumes:
            assert vol.report_storage is False, (
                "DockerRun must defer storage reporting to ConfigFile"
            )

    def test_config_and_volume_share_item_id(self) -> None:
        """The wipe path relies on item_id matching across steps."""
        from openjarvis.installers.recipes.searxng import SEARXNG_INSTALLER

        config_ids = {
            s.item_id
            for s in SEARXNG_INSTALLER.steps
            if isinstance(s, ConfigFile)
        }
        run_step = [
            s for s in SEARXNG_INSTALLER.steps if isinstance(s, DockerRun)
        ][0]
        volume_ids = {v.item_id for v in run_step.volumes}
        assert config_ids & volume_ids  # at least one match

    def test_health_url_uses_json_format(self) -> None:
        """The health check must hit a URL that requires JSON output to
        succeed — that way a misconfigured settings.yml fails fast."""
        from openjarvis.installers.recipes.searxng import (
            HEALTH_URL,
            SEARXNG_INSTALLER,
        )

        assert "format=json" in HEALTH_URL
        wait_step = [
            s for s in SEARXNG_INSTALLER.steps if isinstance(s, WaitForHTTP)
        ][0]
        assert "format=json" in wait_step.url
