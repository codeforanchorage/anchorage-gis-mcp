"""Upstream resilience: readable timeouts, retry inside a per-call budget,
and the item / layer-metadata cache.

Ported from the eCode MCP (plugins/ecode, 1.2.0). The failure these guard:
an httpx timeout stringifies to '', so a slow ArcGIS used to reach the
caller as a bare "Tool execution failed", and a retried timeout (3 x 20s)
could outlive the 28s Lambda and come back as an opaque 502.
"""

import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml

from core.interfaces import ToolInputError
from plugins.anchorage_gis import plugin as gis
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import (
    RETRY_MIN_REMAINING_S,
    TOOL_CALL_BUDGET_S,
    AnchorageGISPlugin,
    UpstreamTimeoutError,
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


class TestReadableErrors:
    @pytest.mark.asyncio
    async def test_empty_exception_names_its_type(self, plugin):
        plugin._find_gis_content = AsyncMock(side_effect=RuntimeError(""))
        result = await plugin.execute_tool("find_gis_content", {"query": "x"})
        assert not result.success
        assert result.error_message == "Tool execution failed (RuntimeError)"

    @pytest.mark.asyncio
    async def test_timeout_is_readable_end_to_end(self, plugin):
        plugin.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
        result = await plugin.execute_tool(
            "get_layer_schema", {"service_url": LAYER}
        )
        assert not result.success
        msg = result.error_message
        assert "did not respond within 20s" in msg
        assert "ReadTimeout" in msg and "services.arcgis.com" in msg
        assert "f=json" not in msg  # query string never leaks

    @pytest.mark.asyncio
    async def test_execute_tool_clears_the_deadline(self, plugin):
        plugin._find_gis_content = AsyncMock(return_value="ok")
        await plugin.execute_tool("find_gis_content", {"query": "x"})
        assert gis._call_deadline.get() is None


# ── _send: retry policy ────────────────────────────────────────────────


class TestSend:
    @pytest.mark.asyncio
    async def test_timeout_never_retried(self, plugin):
        plugin.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
        with pytest.raises(UpstreamTimeoutError, match="within 20s"):
            await plugin._send("get", LAYER, params={})
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_connect_error_retried_then_succeeds(self, plugin):
        good = _resp({"ok": True})
        plugin.client.get = AsyncMock(
            side_effect=[httpx.ConnectError(""), good]
        )
        assert await plugin._send("get", LAYER) is good
        assert plugin.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_connect_error_exhausted_is_readable(self, plugin):
        plugin.client.post = AsyncMock(side_effect=httpx.ConnectError(""))
        with pytest.raises(
            RuntimeError, match=r"Could not reach ArcGIS .* after 3 attempt"
        ) as exc:
            await plugin._send("post", LAYER, data={})
        assert "ConnectError" in str(exc.value)
        assert plugin.client.post.await_count == 3

    @pytest.mark.asyncio
    async def test_5xx_retried_and_final_response_returned(self, plugin):
        plugin.client.get = AsyncMock(return_value=_resp(status=503))
        resp = await plugin._send("get", LAYER)
        # The caller's own status handling still sees the 503.
        assert resp.status_code == 503
        assert plugin.client.get.await_count == 3

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self, plugin):
        plugin.client.get = AsyncMock(return_value=_resp(status=404))
        resp = await plugin._send("get", LAYER)
        assert resp.status_code == 404
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_when_budget_nearly_spent(self, plugin, deadline):
        deadline(RETRY_MIN_REMAINING_S - 1)
        plugin.client.get = AsyncMock(side_effect=httpx.ConnectError("x"))
        with pytest.raises(RuntimeError, match="after 1 attempt"):
            await plugin._send("get", LAYER)
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_spent_budget_makes_no_request(self, plugin, deadline):
        deadline(-1)
        plugin.client.get = AsyncMock()
        with pytest.raises(UpstreamTimeoutError, match="budget"):
            await plugin._send("get", LAYER)
        plugin.client.get.assert_not_called()


class TestRequestTimeout:
    @pytest.mark.asyncio
    async def test_no_override_when_budget_is_ample(self, plugin, deadline):
        deadline(TOOL_CALL_BUDGET_S)
        plugin.client.get = AsyncMock(return_value=_resp({}))
        await plugin._send("get", LAYER, params={"f": "json"})
        assert "timeout" not in plugin.client.get.call_args.kwargs

    @pytest.mark.asyncio
    async def test_caller_cap_passes_through(self, plugin, deadline):
        deadline(TOOL_CALL_BUDGET_S)
        plugin.client.get = AsyncMock(return_value=_resp({}))
        await plugin._send("get", LAYER, timeout=10.0)
        assert plugin.client.get.call_args.kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_shrinks_to_remaining_budget(self, plugin, deadline):
        deadline(5)
        plugin.client.get = AsyncMock(return_value=_resp({}))
        await plugin._send("get", LAYER, timeout=10.0)
        assert 0 < plugin.client.get.call_args.kwargs["timeout"] <= 5


class TestRequestJsonWithRetry:
    @pytest.mark.asyncio
    async def test_timeout_no_longer_retried(self, plugin):
        plugin.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
        with pytest.raises(UpstreamTimeoutError):
            await plugin._request_json_with_retry(f"{LAYER}/query")
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_transient_body_stops_when_budget_low(self, plugin, deadline):
        deadline(RETRY_MIN_REMAINING_S - 1)
        bad = _resp({"error": {"code": 500, "message": ""}})
        plugin.client.get = AsyncMock(return_value=bad)
        with pytest.raises(RuntimeError, match="after 1 attempt"):
            await plugin._request_json_with_retry(f"{LAYER}/query")


# ── item / layer-metadata cache ────────────────────────────────────────


def _item(org=ORG):
    return {"id": ITEM, "orgId": org, "url": LAYER, "type": "Feature Service"}


class TestMetaCache:
    @pytest.mark.asyncio
    async def test_item_fetched_once(self, plugin):
        plugin.client.get = AsyncMock(return_value=_resp(_item()))
        a = await plugin.get_dataset(ITEM)
        b = await plugin.get_dataset(ITEM)
        assert a == b == _item()
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_foreign_item_never_cached(self, plugin):
        plugin.client.get = AsyncMock(return_value=_resp(_item(org="other")))
        for _ in range(2):
            with pytest.raises(ToolInputError, match="not the configured org"):
                await plugin.get_dataset(ITEM)
        assert plugin.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_error_body_never_cached(self, plugin):
        plugin.client.get = AsyncMock(
            side_effect=[
                _resp({"error": {"code": 400, "message": "nope"}}),
                _resp({"fields": []}),
            ]
        )
        with pytest.raises(RuntimeError, match="nope"):
            await plugin._fetch_layer_meta(LAYER)
        assert await plugin._fetch_layer_meta(LAYER) == {"fields": []}

    @pytest.mark.asyncio
    async def test_mutating_result_does_not_poison_cache(self, plugin):
        plugin.client.get = AsyncMock(
            return_value=_resp({"fields": [{"name": "A"}]})
        )
        first = await plugin._fetch_layer_meta(LAYER)
        first["fields"].append({"name": "B"})
        again = await plugin._fetch_layer_meta(LAYER)
        assert again == {"fields": [{"name": "A"}]}

    @pytest.mark.asyncio
    async def test_expired_entry_refetched(self, plugin):
        plugin.META_CACHE_TTL_S = -1  # every entry is born expired
        plugin.client.get = AsyncMock(return_value=_resp({"fields": []}))
        await plugin._fetch_layer_meta(LAYER)
        await plugin._fetch_layer_meta(LAYER)
        assert plugin.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_lru_is_bounded(self, plugin):
        plugin.META_CACHE_MAX_ENTRIES = 2
        plugin.client.get = AsyncMock(return_value=_resp({"fields": []}))
        for i in range(3):
            await plugin._fetch_layer_meta(f"{LAYER[:-1]}{i}")
        assert len(plugin._meta_cache) == 2
        assert f"{LAYER[:-1]}0" not in plugin._meta_cache


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


class TestStaleSchemaRefetch:
    """A cached field list must not reject a field a republish added."""

    @pytest.mark.asyncio
    async def test_missing_required_field_refetches_once(self, plugin):
        plugin.client.get = AsyncMock(return_value=_resp(_meta("Old")))
        await plugin._fetch_layer_meta(LAYER)  # primes the cache
        plugin.client.get = AsyncMock(
            return_value=_resp(_meta("Old", "New"))
        )
        meta = await plugin._fetch_layer_meta(
            LAYER, require_fields=("New",)
        )
        assert {f["name"] for f in meta["fields"]} == {"Old", "New"}
        assert plugin.client.get.await_count == 1
        # The fresh copy replaced the stale one in the cache.
        again = await plugin._fetch_layer_meta(
            LAYER, require_fields=("New",)
        )
        assert {f["name"] for f in again["fields"]} == {"Old", "New"}
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_present_field_is_a_cache_hit(self, plugin):
        plugin.client.get = AsyncMock(return_value=_resp(_meta("A")))
        await plugin._fetch_layer_meta(LAYER)
        await plugin._fetch_layer_meta(LAYER, require_fields=("A",))
        assert plugin.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_typo_costs_one_refetch_then_still_rejects(self, plugin):
        plugin._resolve_layer_url = AsyncMock(return_value=LAYER)
        plugin.client.get = AsyncMock(return_value=_resp(_meta("Zone")))
        await plugin._fetch_layer_meta(LAYER)  # primes the cache
        with pytest.raises(ToolInputError, match="not a field"):
            await plugin._fetch_aggregation_polygons(ITEM, "zone", "1=1")
        assert plugin.client.get.await_count == 2  # prime + one refetch


class TestFieldInventoryPartial:
    """An unreadable service must not read as 'no such field'."""

    @pytest.mark.asyncio
    async def test_timeouts_are_reported_not_hidden(self, plugin):
        service = LAYER.rsplit("/", 1)[0]
        plugin._run_search = AsyncMock(
            return_value=[{"url": service, "title": "Svc", "id": ITEM}]
        )
        plugin.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
        text = await plugin._search_layers_by_field(
            {"field_keyword": "zone"}
        )
        assert "None of the 1" in text
        assert "Partial inspection" in text
        assert "1 service(s)" in text

    @pytest.mark.asyncio
    async def test_clean_miss_has_no_partial_note(self, plugin):
        service = LAYER.rsplit("/", 1)[0]
        plugin._run_search = AsyncMock(
            return_value=[{"url": service, "title": "Svc", "id": ITEM}]
        )
        plugin.client.get = AsyncMock(
            side_effect=[
                _resp({"layers": [{"id": 0}]}),
                _resp(_meta("Other")),
            ]
        )
        text = await plugin._search_layers_by_field(
            {"field_keyword": "zone"}
        )
        assert "None of the 1" in text
        assert "Partial inspection" not in text
