"""Checks that THIS deployment's config agrees with its infrastructure.

Unlike the rest of the suite these are about the Anchorage GIS
deployment, not the shared core: they read the real config.yaml and
terraform/aws/prod.tfvars and fail when the two drift apart.
"""

import re
from pathlib import Path

import pytest

from core.validators import get_http_settings, load_and_validate_config

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_TFVARS = REPO_ROOT / "terraform" / "aws" / "prod.tfvars"
CONFIG_FILES = ("config.yaml", "config-anchorage-gis.yaml")
GCC_PATH = "/mcp-gcc"


def _gcc_route_enabled() -> bool:
    match = re.search(
        r"^\s*enable_gcc_route\s*=\s*(true|false)\b",
        PROD_TFVARS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return bool(match) and match.group(1) == "true"


@pytest.mark.parametrize("config_name", CONFIG_FILES)
class TestMcpRoutes:
    """The handler 404s any path the config does not list, so a route
    API Gateway forwards must also be in `server_mcp_paths`."""

    def test_public_route_is_served(self, config_name):
        config = load_and_validate_config(str(REPO_ROOT / config_name))
        assert "/mcp" in get_http_settings(config)["mcp_paths"]

    def test_gcc_route_matches_terraform(self, config_name):
        """enable_gcc_route = true creates /mcp-gcc at the gateway; if
        the config does not list the path too, the Copilot connector gets
        a 404 from the Lambda behind a route that looks healthy."""
        config = load_and_validate_config(str(REPO_ROOT / config_name))
        served = GCC_PATH in get_http_settings(config)["mcp_paths"]
        assert served == _gcc_route_enabled()
