"""Reusable installer primitives — Docker, files, etc.

Each primitive is a step you compose into an :class:`Installer` recipe.
Primitives are intentionally small and orthogonal so a new connector's
install flow is mostly declarative.
"""

from openjarvis.installers.primitives.docker import (
    DockerEnvCheck,
    DockerImage,
    DockerRun,
    WaitForHTTP,
    docker_available,
    docker_cli_present,
    docker_installed,
    list_managed_images,
    start_docker_desktop,
)
from openjarvis.installers.primitives.files import ConfigFile

__all__ = [
    "ConfigFile",
    "DockerEnvCheck",
    "DockerImage",
    "DockerRun",
    "WaitForHTTP",
    "docker_available",
    "docker_cli_present",
    "docker_installed",
    "list_managed_images",
    "start_docker_desktop",
]
