"""Upstream resilience: readable timeouts, retry inside a per-call budget,
and the item / layer-metadata cache.

Ported from the eCode MCP (plugins/ecode, 1.2.0). The failure these guard:
an httpx timeout stringifies to '', so a slow ArcGIS used to reach the
caller as a bare "Tool execution failed", and a retried timeout (3 x 20s)
could outlive the 28s Lambda and come back as an opaque 502.
"""

import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from plugins.anchorage_gis import plugin as gis
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import (
    TOOL_CALL_BUDGET_S,
    AnchorageGISPlugin,
)

ORG = "Ce3DhLRthdwbHlfF"
ITEM = "0123456789abcdef0123456789abcdef"
LAYER = "https://services.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/X/FeatureServer/0"


@pytest.fixture
def plugin():
    cfg = {
        "portal_base_url": "https://muniorg.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "c34ed10758ec4f4eb8aa6826ee5be3ff",
        "org_id": ORG,
        "city_name": "Municipality of Anchorage",
        "gallery_url": "https://example.org/gallery",
        "timeout": 20,
    }
    p = AnchorageGISPlugin(cfg)
    p.plugin_config = AnchorageGISPluginConfig(**cfg)
    p.ARCGIS_RETRY_BACKOFF_S = 0  # no real sleeping in tests
    p.client = Mock()
    return p


def _resp(payload=None, status=200):
    r = Mock()
    r.status_code = status
    r.text = str(payload)
    r.json.return_value = payload if payload is not None else {}
    r.headers = {"content-type": "application/json"}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=Mock(), response=r
        )
    return r


@pytest.fixture
def deadline():
    """Set the call deadline as execute_tool would, `seconds` from now.

    Called inside the async test, so the value lives in that test's own
    context and cannot leak into another test.
    """

    def _set(seconds):
        gis._call_deadline.set(time.monotonic() + seconds)

    return _set


# ── readable errors ────────────────────────────────────────────────────


# ── _send: retry policy ────────────────────────────────────────────────


# ── item / layer-metadata cache ────────────────────────────────────────


def _item(org=ORG):
    return {"id": ITEM, "orgId": org, "url": LAYER, "type": "Feature Service"}


# ── the budget fits the deployed timeout ladder ────────────────────────


class TestBudgetFitsDeployment:
    @pytest.fixture
    def config(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_budget_ends_before_the_lambda(self, config):
        lambda_timeout = config["aws"]["lambda_timeout"]
        assert lambda_timeout - TOOL_CALL_BUDGET_S >= 3

    def test_budget_covers_one_full_request(self, config):
        assert (
            TOOL_CALL_BUDGET_S > config["plugins"]["anchorage_gis"]["timeout"]
        )


# ── review fixes: stale-schema refetch, partial field inventory ────────


def _meta(*names):
    return {
        "geometryType": "esriGeometryPolygon",
        "fields": [{"name": n, "type": "esriFieldTypeString"} for n in names],
    }


