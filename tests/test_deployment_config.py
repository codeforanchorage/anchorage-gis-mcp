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


def _tfvar_int(name: str) -> int:
    """Read a bare integer variable out of prod.tfvars."""
    text = PROD_TFVARS.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    assert match, f"{name} not found in {PROD_TFVARS}"
    return int(match.group(1))


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


API_GATEWAY_TF = REPO_ROOT / "terraform" / "aws" / "api_gateway.tf"


class TestCapacity:
    """Throttle and concurrency settings that only bite under real load.

    Ported from anchorage-ecode-mcp c6ca0e2.
    """

    def test_stage_throttle_reads_the_tfvars(self):
        """The stage-wide method settings must use the tfvars, not literals.

        They are the ONLY throttle on the unkeyed public /mcp route (the
        usage plan applies to API-key requests, i.e. /mcp-gcc). With
        literals here, raising api_*_limit in prod.tfvars would silently
        change nothing for real users.
        """
        text = API_GATEWAY_TF.read_text(encoding="utf-8")
        block = re.search(
            r'resource "aws_api_gateway_method_settings" "mcp_post" \{'
            r".*?\n\}",
            text,
            re.DOTALL,
        )
        assert block, "aws_api_gateway_method_settings.mcp_post not found"
        body = block.group(0)
        assert re.search(
            r"throttling_burst_limit\s*=\s*var\.api_burst_limit", body
        )
        assert re.search(
            r"throttling_rate_limit\s*=\s*var\.api_rate_limit", body
        )

    def test_burst_at_least_rate(self):
        assert _tfvar_int("api_burst_limit") >= _tfvar_int("api_rate_limit")

    def test_reserved_concurrency_covers_burst(self):
        """Every request the gateway admits in a burst should find a Lambda
        slot; otherwise the overflow surfaces as Lambda throttles (502s)
        rather than clean gateway 429s."""
        assert _tfvar_int("lambda_reserved_concurrency") >= _tfvar_int(
            "api_burst_limit"
        )
