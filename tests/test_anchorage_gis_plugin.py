"""Tests for Anchorage GIS plugin.

Verifies plugin initialization, tool definitions, tool execution,
error handling, and data formatting.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch


from core.interfaces import PluginType, ToolInputError
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin


@pytest.fixture
def anchorage_config():
    """Standard Anchorage GIS plugin configuration."""
    return {
        "portal_base_url": "https://muniorg.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "c34ed10758ec4f4eb8aa6826ee5be3ff",
        "org_id": "Ce3DhLRthdwbHlfF",
        "city_name": "Municipality of Anchorage",
        "gallery_url": (
            "https://muniorg.maps.arcgis.com/apps/instant/filtergallery/"
            "index.html?appid=4dac7569f1cc4beb9f22ce168c899a30"
        ),
        "timeout": 30,
    }


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        assert plugin.plugin_name == "anchorage_gis"
        assert plugin.plugin_type == PluginType.OPEN_DATA
        assert plugin.plugin_version == "1.2.0"


# ── Initialization ─────────────────────────────────────────────────────


# ── get_tools ──────────────────────────────────────────────────────────


class TestGetTools:
    def test_get_tools_returns_all_tools(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        tools = plugin.get_tools()

        assert len(tools) == 16
        tool_names = [t.name for t in tools]
        assert "footprint_for_parcel" in tool_names
        assert "find_gis_content" in tool_names
        assert "browse_gallery" in tool_names
        assert "search_spatial_layers" in tool_names
        assert "get_item_details" in tool_names
        assert "get_layer_schema" in tool_names
        assert "get_distinct_values" in tool_names
        assert "find_parcel" in tool_names
        assert "search_layers_by_field" in tool_names
        assert "query_data" in tool_names
        assert "spatial_query_point" in tool_names
        assert "spatial_query_polygon" in tool_names
        assert "aggregate_by_polygon" in tool_names
        assert "coverage_by_polygon" in tool_names
        assert "filter_by_polygon" in tool_names
        assert "find_features_spanning_classifications" in tool_names


# ── execute_tool ───────────────────────────────────────────────────────


# ── query_data two-hop resolution ─────────────────────────────────────


# ── spatial_query_point ────────────────────────────────────────────────


def _spatial_client_mock(layer_meta, query_features):
    """Build a mock httpx client that returns layer metadata on the
    first GET (the service root) and query features on the second GET
    (the /query endpoint). Both responses are JSON."""
    meta_resp = Mock()
    meta_resp.status_code = 200
    meta_resp.raise_for_status = Mock()
    meta_resp.headers = {"content-type": "application/json"}
    meta_resp.json.return_value = layer_meta

    query_resp = Mock()
    query_resp.status_code = 200
    query_resp.raise_for_status = Mock()
    query_resp.headers = {"content-type": "application/json"}
    query_resp.json.return_value = {"features": query_features}

    client = AsyncMock()
    client.get = AsyncMock(side_effect=[meta_resp, query_resp])
    return client


# ── Geometry helpers ───────────────────────────────────────────────────


# ── aggregate_by_polygon ───────────────────────────────────────────────


def _ok_resp(payload):
    r = Mock()
    r.status_code = 200
    r.raise_for_status = Mock()
    r.headers = {"content-type": "application/json"}
    r.json.return_value = payload
    return r


# 32-char hex item IDs used across these tests
_SOURCE_ID = "a" * 32
_AGG_ID = "b" * 32
_CONTAINER_ID = "c" * 32


# ── filter_by_polygon ──────────────────────────────────────────────────


# ── Security / private-data / DoS threat model ────────────────────────


# ── Item ownership check ───────────────────────────────────────────────


_OTHER_ORG = "OtherOrgIdNotMOA1234567890abcde2"


# ── Service URL allowlist ──────────────────────────────────────────────


# ── Layer URL helper ───────────────────────────────────────────────────


# ── Formatters ─────────────────────────────────────────────────────────


class TestAnchorageCoveragePct:
    def test_wgs84_full_overlap(self):
        # Bbox exactly the muni — coverage close to 1.0.
        extent = {
            "xmin": -150.5, "ymin": 60.5,
            "xmax": -148.5, "ymax": 61.6,
            "spatialReference": {"wkid": 4326},
        }
        pct = AnchorageGISPlugin._anchorage_coverage_pct(extent)
        assert pct is not None
        assert 0.95 <= pct <= 1.05

    def test_wgs84_partial_overlap(self):
        # Bbox covers only a small slice of downtown Anchorage.
        extent = {
            "xmin": -149.95, "ymin": 61.18,
            "xmax": -149.85, "ymax": 61.22,
            "spatialReference": {"wkid": 4326},
        }
        pct = AnchorageGISPlugin._anchorage_coverage_pct(extent)
        assert pct is not None
        assert 0 < pct < 0.05

    def test_wgs84_no_overlap(self):
        # Florida — far from Anchorage, should be 0.
        extent = {
            "xmin": -82, "ymin": 25, "xmax": -80, "ymax": 27,
            "spatialReference": {"wkid": 4326},
        }
        pct = AnchorageGISPlugin._anchorage_coverage_pct(extent)
        assert pct == 0.0

    def test_web_mercator_handled(self):
        # Web Mercator bbox roughly over Anchorage downtown.
        # -149.9, 61.2 in WGS84 ≈ -16685000, 8666000 in Web Mercator.
        extent = {
            "xmin": -16700000, "ymin": 8650000,
            "xmax": -16600000, "ymax": 8700000,
            "spatialReference": {"wkid": 102100},
        }
        pct = AnchorageGISPlugin._anchorage_coverage_pct(extent)
        assert pct is not None
        assert pct > 0

    def test_unhandled_sr_returns_none(self):
        # Alaska Albers — we don't convert, should bail out.
        extent = {
            "xmin": 100000, "ymin": 1200000,
            "xmax": 200000, "ymax": 1300000,
            "spatialReference": {"wkid": 3338},
        }
        pct = AnchorageGISPlugin._anchorage_coverage_pct(extent)
        assert pct is None

    def test_malformed_extent_returns_none(self):
        assert AnchorageGISPlugin._anchorage_coverage_pct(None) is None
        assert AnchorageGISPlugin._anchorage_coverage_pct({}) is None
        assert AnchorageGISPlugin._anchorage_coverage_pct(
            {"xmin": "bad"}
        ) is None

    def test_degenerate_bbox_returns_none(self):
        # Single-point layer (e.g. one hospital). xmin == xmax,
        # ymin == ymax. The math would say "0% overlap" because the
        # intersection has zero area — but the point IS inside
        # Anchorage. Return None so the caveat stays silent rather
        # than emitting a misleading "does not overlap" message.
        for extent in (
            {  # both dims degenerate
                "xmin": -149.9, "ymin": 61.2,
                "xmax": -149.9, "ymax": 61.2,
                "spatialReference": {"wkid": 4326},
            },
            {  # zero-width
                "xmin": -149.9, "ymin": 61.0,
                "xmax": -149.9, "ymax": 61.3,
                "spatialReference": {"wkid": 4326},
            },
            {  # zero-height
                "xmin": -150.0, "ymin": 61.2,
                "xmax": -149.0, "ymax": 61.2,
                "spatialReference": {"wkid": 4326},
            },
        ):
            assert (
                AnchorageGISPlugin._anchorage_coverage_pct(extent) is None
            )


# ── Config schema ──────────────────────────────────────────────────────


class TestNormalizeParcelVariants:
    """Pure normaliser for MOA parcel IDs. Generates the four
    canonical formats (8-digit compact + hyphenated, 11-digit
    compact + hyphenated) from any common input form."""

    def test_hyphenated_8digit_input(self):
        # Input "001-213-29" — MOA-canonical short hyphenated form.
        out = AnchorageGISPlugin._normalize_parcel_variants("001-213-29")
        assert "00121329" in out
        assert "001-213-29" in out
        assert "00121329000" in out
        assert "001-213-29-000" in out

    def test_compact_8digit_input_pads_with_default_sub(self):
        # Compact 8-digit input — sub-parcel defaults to "000".
        out = AnchorageGISPlugin._normalize_parcel_variants("00121329")
        assert "00121329" in out
        assert "001-213-29" in out
        assert "00121329000" in out
        assert "001-213-29-000" in out

    def test_compact_11digit_input_preserves_sub(self):
        # Real sub-parcel "001" should round-trip in the variants.
        out = AnchorageGISPlugin._normalize_parcel_variants("00121329001")
        assert "00121329001" in out
        assert "001-213-29-001" in out
        # Both 8-digit forms should also be present (so model can find
        # the parent parcel across layers that drop the sub).
        assert "00121329" in out
        assert "001-213-29" in out

    def test_input_with_leading_zero_dropped(self):
        # User typed "1-213-29" — 6 digits, missing leading zeros.
        # Must still recover the canonical "001-213-29" form.
        out = AnchorageGISPlugin._normalize_parcel_variants("1-213-29")
        assert "001-213-29" in out
        assert "00121329" in out

    def test_input_with_prefix_text(self):
        # Real-world: "Parcel 003-184-87". Text prefix should not
        # break extraction.
        out = AnchorageGISPlugin._normalize_parcel_variants(
            "Parcel 003-184-87"
        )
        assert "00318487" in out
        assert "003-184-87" in out
        assert "00318487000" in out

    def test_too_short_returns_empty(self):
        # < 5 digits is too ambiguous to normalise; refuse rather than
        # generate misleading variants.
        assert AnchorageGISPlugin._normalize_parcel_variants("12") == []
        assert AnchorageGISPlugin._normalize_parcel_variants("") == []
        assert AnchorageGISPlugin._normalize_parcel_variants(None) == []

    def test_no_digits_returns_empty(self):
        assert AnchorageGISPlugin._normalize_parcel_variants(
            "no digits here"
        ) == []


    # -- digits from around the id must not be run into it --

    @pytest.mark.parametrize(
        "raw",
        [
            "Lot 5 Block 12, 003-184-87",
            "003-184-87 (Lot 5, Block 12)",
            "PID: 00318487",
            "parcel #003-184-87-000.",
        ],
    )
    def test_surrounding_text_does_not_change_the_id(self, raw):
        out = AnchorageGISPlugin._normalize_parcel_variants(raw)
        assert "00318487" in out
        assert "003-184-87-000" in out
        assert not any("512" in v for v in out if v != raw.strip())

    @pytest.mark.parametrize(
        "raw",
        [
            "1820 Parkside Dr 99501",  # an address is not a parcel id
            "1820 Parkside Dr, Anchorage, AK 99501-1234",
            "003-184-87 or 001-213-29",  # two ids: which one?
            "Lot 5 Block 12",
            "0031848700099",  # 13 digits: not a parcel number
            "Parcel 1-213-29",  # short form only when it stands alone
            "A00318487",  # part of a longer identifier
        ],
    )
    def test_unreadable_input_is_refused(self, raw):
        assert AnchorageGISPlugin._normalize_parcel_variants(raw) == []

    @pytest.mark.parametrize(
        "raw, base, full",
        [
            ("003 184 87", "00318487", "00318487000"),
            ("003.184.87", "00318487", "00318487000"),
            ("318487000", "00318487", "00318487000"),
            ("3-184-87-001", "00318487", "00318487001"),
        ],
    )
    def test_id_alone_in_any_spelling(self, raw, base, full):
        out = AnchorageGISPlugin._normalize_parcel_variants(raw)
        assert base in out and full in out


class TestFindParcelCandidates:
    """The near-miss search behind a find_parcel that matched nothing."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        p._resolve_layer_url = AsyncMock(return_value="https://x/0")
        p._fetch_layer_meta = AsyncMock(
            return_value={"fields": [{"name": "Parcel_Num"}]}
        )
        return p

    @staticmethod
    def _resp(payload):
        r = Mock()
        r.status_code = 200
        r.raise_for_status = Mock()
        r.json.return_value = payload
        return r

    async def _miss(self, plugin, second, parcel_id="003-184-87"):
        effects = [self._resp({"features": []})]
        if len(AnchorageGISPlugin._parcel_digits(parcel_id) or "") >= 9:
            # An 11-digit number is tried alone, then by its base.
            effects.append(self._resp({"features": []}))
        if second is not None:
            effects.append(
                second
                if isinstance(second, Exception)
                else self._resp(second)
            )
        plugin._send = AsyncMock(side_effect=effects)
        return await plugin._find_parcel(
            {
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": parcel_id,
            }
        )

    @pytest.mark.asyncio
    async def test_searches_compact_and_hyphenated_forms(self, plugin):
        await self._miss(plugin, {"features": []})
        params = plugin._send.await_args_list[1].kwargs["params"]
        assert "LIKE '%318487%'" in params["where"]
        assert "LIKE '%3-184-87%'" in params["where"]
        assert params["orderByFields"] == "Parcel_Num"

    @pytest.mark.asyncio
    async def test_search_uses_the_parcel_base_not_typed_digits(
        self, plugin
    ):
        # 11-digit input with a sub-parcel: the base is what near
        # misses share, and text around the id plays no part.
        await self._miss(
            plugin, {"features": []}, parcel_id="Parcel 003-184-87-001"
        )
        where = plugin._send.await_args_list[-1].kwargs["params"]["where"]
        assert "'%318487%'" in where and "001" not in where

    @pytest.mark.asyncio
    async def test_repeated_values_are_listed_once(self, plugin):
        rows = [{"attributes": {"Parcel_Num": "00318487001"}}] * 7 + [
            {"attributes": {"Parcel_Num": "00318487002"}}
        ]
        text, structured = await self._miss(plugin, {"features": rows})
        (caveat,) = structured["caveats"]
        assert caveat["candidates"] == [
            {"Parcel_Num": "00318487001"},
            {"Parcel_Num": "00318487002"},
        ]
        assert caveat["count"] == 2
        assert "there may be more" not in caveat["message"]
        assert text.count("`00318487001`") == 1

    @pytest.mark.asyncio
    async def test_more_than_five_values_says_so(self, plugin):
        rows = [
            {"attributes": {"Parcel_Num": f"0031848700{i}"}}
            for i in range(8)
        ]
        text, structured = await self._miss(plugin, {"features": rows})
        (caveat,) = structured["caveats"]
        assert len(caveat["candidates"]) == 5
        assert "there may be more" in caveat["message"]
        assert "cut short" in text

    @pytest.mark.asyncio
    async def test_failed_search_is_not_reported_as_absence(self, plugin):
        text, structured = await self._miss(
            plugin, RuntimeError("upstream HTTP 503")
        )
        (caveat,) = structured["caveats"]
        assert caveat["code"] == "parcel_not_found"
        assert "could not be completed" in caveat["message"]
        assert "not evidence the parcel is absent" in caveat["message"]
        assert "may not exist" not in caveat["message"]
        assert "may not exist" not in text
        assert "candidates" not in caveat

    @pytest.mark.asyncio
    async def test_error_body_is_a_failed_search(self, plugin):
        _, structured = await self._miss(
            plugin, {"error": {"code": 400, "message": "Invalid query"}}
        )
        assert "Invalid query" in structured["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_too_few_digits_skips_the_search(self, plugin):
        # 00000123 leaves "123": it matches thousands of parcels.
        _, structured = await self._miss(plugin, None, parcel_id="00123")
        assert plugin._send.await_count == 1
        assert "candidates" not in structured["caveats"][0]

    @pytest.mark.asyncio
    async def test_address_is_refused_before_any_request(self, plugin):
        plugin._send = AsyncMock()
        with pytest.raises(ToolInputError, match="not a parcel ID"):
            await plugin._find_parcel(
                {
                    "item_id": "a" * 32,
                    "parcel_field": "Parcel_Num",
                    "parcel_id": "1820 Parkside Dr 99501",
                }
            )
        plugin._send.assert_not_called()


class TestFindParcelSubParcel:
    """An 11-digit number names one record; a base number, all of them."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        p._resolve_layer_url = AsyncMock(return_value="https://x/0")
        return p

    def _serve(self, plugin, field, stored):
        """Answer IN lookups from `stored`, the values the field holds."""
        plugin._fetch_layer_meta = AsyncMock(
            return_value={"fields": [{"name": field}]}
        )
        wheres = []

        async def fake_send(method, url, params=None, **kw):
            where = params["where"]
            wheres.append(where)
            r = Mock()
            r.status_code = 200
            r.raise_for_status = Mock()
            r.json.return_value = {
                "features": [
                    {"attributes": {field: v}}
                    for v in stored
                    if f"'{v}'" in where
                ]
            }
            return r

        plugin._send = fake_send
        return wheres

    async def _find(self, plugin, field, parcel_id):
        return await plugin._find_parcel(
            {
                "item_id": "a" * 32,
                "parcel_field": field,
                "parcel_id": parcel_id,
                "limit": 50,
            }
        )

    UNITS11 = [f"012182040{i:02d}" for i in range(1, 4)]

    @pytest.mark.asyncio
    async def test_unit_on_an_11_digit_field_is_that_unit(self, plugin):
        wheres = self._serve(plugin, "Parcel_ID", self.UNITS11)
        text, s = await self._find(plugin, "Parcel_ID", "012-182-04-002")
        assert s["summary"]["matched_values"] == ["01218204002"]
        assert s["caveats"] == []
        assert len(wheres) == 1 and "'01218204'" not in wheres[0]

    @pytest.mark.asyncio
    async def test_unit_on_an_8_digit_field_says_base_only(self, plugin):
        # The field cannot tell units apart: every row is the base.
        self._serve(plugin, "GIS_ParcelNum8", ["01218204"] * 3)
        text, s = await self._find(plugin, "GIS_ParcelNum8", "012-182-04-002")
        assert s["summary"]["returned"] == 3
        (caveat,) = s["caveats"]
        assert caveat["code"] == "base_number_match"
        assert "not the sub-parcel `002`" in caveat["message"]
        assert "matched on the base number, not the sub-parcel" in text
        assert "11-digit field" in text

    @pytest.mark.asyncio
    async def test_missing_sub_parcel_says_base_only(self, plugin):
        self._serve(plugin, "Parcel_Num", ["00215132"])
        _, s = await self._find(plugin, "Parcel_Num", "002-151-32-999")
        assert [c["code"] for c in s["caveats"]] == ["base_number_match"]

    @pytest.mark.asyncio
    async def test_sub_000_under_its_base_is_not_flagged(self, plugin):
        # 00215132000 IS parcel 002-151-32; an 8-digit field holding it
        # as 00215132 has found the parcel asked for.
        wheres = self._serve(plugin, "GIS_ParcelNum8", ["00215132"])
        text, s = await self._find(plugin, "GIS_ParcelNum8", "00215132000")
        assert s["summary"]["matched"] is True
        assert s["caveats"] == []
        assert "found" in text.splitlines()[0]
        assert len(wheres) == 2

    @pytest.mark.asyncio
    async def test_base_number_is_one_lookup_of_all_forms(self, plugin):
        wheres = self._serve(plugin, "Parcel_ID", ["00215132000"])
        _, s = await self._find(plugin, "Parcel_ID", "002-151-32")
        assert s["summary"]["canonical"] == "00215132000"
        assert len(wheres) == 1
        for v in ("00215132", "002-151-32", "00215132000", "002-151-32-000"):
            assert f"'{v}'" in wheres[0]


class TestFootprintOverlayRecords:
    """A Lease or Economic record is not a lot, and is not "not found"."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        p._resolve_layer_url = AsyncMock(return_value="https://x/0")
        return p

    ECONOMIC = {
        "attributes": {
            "Parcel_ID": "00318487000",
            "GIS_Category": "Economic",
            "Parcel_Address": "1607 E 27TH AVE",
        }
    }

    @pytest.mark.asyncio
    async def test_economic_record_is_named_as_such(self, plugin):
        plugin._request_json_with_retry = AsyncMock(
            side_effect=[{"features": []}, {"features": [self.ECONOMIC]}]
        )
        text, s = await plugin._footprint_for_parcel(
            {"parcel_id": "003-184-87"}
        )
        assert "is not a land parcel" in text
        assert "no parcel found" not in text
        assert "1607 E 27TH AVE" in text
        assert s["result"] is None
        (caveat,) = s["caveats"]
        assert caveat["code"] == "not_a_land_parcel"
        assert caveat["candidates"][0]["category"] == "Economic"
        second = plugin._request_json_with_retry.await_args_list[1]
        assert "GIS_Category" not in second.kwargs["data"]["where"]

    @pytest.mark.asyncio
    async def test_unknown_number_is_still_not_found(self, plugin):
        plugin._request_json_with_retry = AsyncMock(
            side_effect=[{"features": []}, {"features": []}]
        )
        text, s = await plugin._footprint_for_parcel(
            {"parcel_id": "999-999-99"}
        )
        assert "no parcel found" in text
        assert s["caveats"][0]["code"] == "parcel_not_found"

    @pytest.mark.asyncio
    async def test_failed_second_lookup_falls_back_to_not_found(
        self, plugin
    ):
        plugin._request_json_with_retry = AsyncMock(
            side_effect=[{"features": []}, RuntimeError("upstream HTTP 503")]
        )
        text, s = await plugin._footprint_for_parcel(
            {"parcel_id": "003-184-87"}
        )
        assert "no parcel found" in text
        assert s["caveats"][0]["code"] == "parcel_not_found"


class TestFindParcel:
    """Integration of the parcel normaliser with a real WHERE IN
    lookup. Validates that the variants are sent to the upstream
    correctly and that the response surfaces the canonical stored
    form for follow-up queries."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_in_clause_carries_all_variants(self, plugin):
        # Verify the WHERE IN actually contains every generated form,
        # so a layer storing any of them would match.
        captured = {}

        async def fake_get(url, params=None):
            captured.update(params or {})
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            resp.json.return_value = {
                "features": [
                    {"attributes": {"Parcel_Num": "00121329000"}}
                ]
            }
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get

            await plugin._find_parcel({
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "001-213-29",
            })

        where = captured["where"]
        # All four canonical forms must be in the IN clause.
        assert "'00121329'" in where
        assert "'001-213-29'" in where
        assert "'00121329000'" in where
        assert "'001-213-29-000'" in where
        assert "Parcel_Num IN" in where

    @pytest.mark.asyncio
    async def test_match_surfaces_canonical_stored_form(self, plugin):
        # The layer happens to store 11-digit compact. Response should
        # tell the model what stored format matched, so follow-up
        # queries on this layer use the right form verbatim.
        async def fake_get(url, params=None):
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            resp.json.return_value = {
                "features": [
                    {
                        "attributes": {
                            "Parcel_Num": "00318487000",
                            "Source": "P-464",
                        }
                    }
                ]
            }
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get

            text, structured = await plugin._find_parcel({
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "003-184-87",
            })

        assert "00318487000" in text
        assert "Canonical form for this layer" in text
        assert "P-464" in text  # other fields surfaced too
        # The structured half carries the same answer for clients that
        # show only structuredContent.
        assert structured["summary"]["matched"] is True
        assert structured["summary"]["canonical"] == "00318487000"
        assert structured["rows"][0]["Parcel_Num"] == "00318487000"

    @pytest.mark.asyncio
    async def test_no_match_falls_back_to_like(self, plugin):
        # First call (IN) returns no features; second call (LIKE)
        # returns 2 candidates. The response should surface the
        # candidates so the model has something to act on.
        call_count = {"n": 0}

        async def fake_get(url, params=None):
            call_count["n"] += 1
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            if call_count["n"] == 1:
                # Exact-match IN returns nothing.
                resp.json.return_value = {"features": []}
            else:
                # LIKE fallback finds candidates.
                resp.json.return_value = {
                    "features": [
                        {"attributes": {"Parcel_Num": "00121329111"}},
                        {"attributes": {"Parcel_Num": "00121329222"}},
                    ]
                }
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get

            text, structured = await plugin._find_parcel({
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "001-213-29",
            })

        assert "no exact match" in text
        assert "LIKE fallback" in text
        assert "00121329111" in text
        assert structured["summary"]["matched"] is False
        assert structured["summary"]["canonical"] is None
        (caveat,) = structured["caveats"]
        assert caveat["code"] == "parcel_not_found"
        assert {"Parcel_Num": "00121329111"} in caveat["candidates"]
        assert "00121329222" in text

    @pytest.mark.asyncio
    async def test_unknown_parcel_field_names_recovery(self, plugin):
        # Bad field name should give the model a clear path to recover
        # — same UX pattern as the rest of the plugin's errors.
        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            with pytest.raises(ValueError, match="get_layer_schema"):
                await plugin._find_parcel({
                    "item_id": "a" * 32,
                    "parcel_field": "made_up_field",
                    "parcel_id": "001-213-29",
                })


# ── Proximity buffer support ───────────────────────────────────────────


# ── coverage_by_polygon ────────────────────────────────────────────────


_COV_META = {
    "geometryType": "esriGeometryPolygon",
    "fields": [
        {"name": "Parcel_ID", "type": "esriFieldTypeString"},
        {"name": "Shape__Area", "type": "esriFieldTypeDouble"},
    ],
}
_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
}


def _cov_resp(cov_sum):
    r = Mock()
    r.status_code = 200
    r.raise_for_status = Mock()
    r.headers = {"content-type": "application/json"}
    r.json.return_value = {"features": [{"attributes": {"cov_sum": cov_sum}}]}
    return r


# ── transient-error retry hardening ────────────────────────────────────


def _json_resp(payload, status=200):
    r = Mock()
    r.status_code = status
    r.text = str(payload)
    r.json.return_value = payload
    r.headers = {"content-type": "application/json"}
    return r


# ── footprint_for_parcel ───────────────────────────────────────────────


def _fp_parcel_attrs(**overrides):
    """Baseline parcel attributes for footprint_for_parcel tests.

    Geometry defaults to a 20x20m square (400 m2 = ~4,306 sqft), so
    Lot_Size=4306 keeps the mapped-vs-assessor cross-check quiet.
    """
    attrs = {
        "Parcel_ID": "00326477000",
        "GIS_ParcelNum11": "00326477000",
        "Parcel_Address": "1820 PARKSIDE DR",
        "Property_Type": "Residential",
        "Land_Use": "Residential 1 Family",
        "Total_Living_Units": 1,
        "Lot_Size": 4306,
        "Zoning_District": "R2D",
        "Grid_Map": "SW1433",
        "Condo_Unit_Number": None,
        "Parcel_ID_URL": "https://property.muni.org/x?pin=00326477000",
    }
    attrs.update(overrides)
    return attrs


_FP_SQUARE_RINGS = [[[0, 0], [0, 20], [20, 20], [20, 0], [0, 0]]]


def _fp_building(coords):
    return {
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {"OBJECTID": 1, "Category": "General"},
    }


async def _fp_run(
    plugin, parcel_features, bldg_features, parcel_id="00326477000"
):
    """Run _footprint_for_parcel with the upstream calls mocked out."""
    with patch.object(
        plugin,
        "_resolve_layer_url",
        new_callable=AsyncMock,
        return_value="https://services2.arcgis.com/x/FeatureServer/0",
    ), patch.object(
        plugin,
        "_request_json_with_retry",
        new_callable=AsyncMock,
        return_value={"features": parcel_features},
    ), patch.object(
        plugin,
        "_paged_geojson_fetch",
        new_callable=AsyncMock,
        return_value=bldg_features,
    ):
        # Prose only: these tests assert on the rendering. The
        # structured half is covered in test_structured_output.py.
        text, _ = await plugin._footprint_for_parcel(
            {"parcel_id": parcel_id}
        )
        return text


class TestFootprintForParcel:
    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        p.client = AsyncMock()
        return p

    # -- ambiguous match: candidates must be in the STRUCTURED half --

    @pytest.mark.asyncio
    async def test_ambiguous_match_lists_candidates_structured(
        self, plugin
    ):
        two = [
            {"attributes": _fp_parcel_attrs(
                Parcel_ID="00908111000",
                Parcel_Address="3600 DENALI ST",
            )},
            {"attributes": _fp_parcel_attrs(
                Parcel_ID="00908111001",
                Parcel_Address="3600 DENALI ST",
                Condo_Unit_Number="1",
            )},
        ]
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services2.arcgis.com/x/FeatureServer/0",
        ), patch.object(
            plugin,
            "_request_json_with_retry",
            new_callable=AsyncMock,
            return_value={"features": two},
        ):
            text, structured = await plugin._footprint_for_parcel(
                {"parcel_id": "009-081-11"}
            )
        assert structured["result"] is None
        caveat = structured["caveats"][0]
        assert caveat["code"] == "parcel_ambiguous"
        # Structured-output clients render only structuredContent, so
        # the candidate Parcel_IDs must live there, not just in the
        # markdown half.
        ids = [c["parcel_id"] for c in caveat["candidates"]]
        assert ids == ["00908111000", "00908111001"]
        assert "00908111000" in caveat["message"]
        assert "00908111000" in text

    # -- zoning normalization / cap table --

    def test_normalize_bowl_district(self):
        assert AnchorageGISPlugin._normalize_zoning_district("R2D") == (
            "R2D",
            "bowl",
        )

    def test_normalize_strips_sl_suffix(self):
        assert AnchorageGISPlugin._normalize_zoning_district("R6SL") == (
            "R6",
            "bowl",
        )

    def test_normalize_ce_prefix_with_hyphen_and_space(self):
        assert AnchorageGISPlugin._normalize_zoning_district("CE-R1A") == (
            "R1A",
            "chugiak-eagle river",
        )
        assert AnchorageGISPlugin._normalize_zoning_district("CE RO") == (
            "RO",
            "chugiak-eagle river",
        )

    def test_normalize_girdwood_prefix(self):
        assert AnchorageGISPlugin._normalize_zoning_district("GR1") == (
            "R1",
            "girdwood",
        )

    def test_normalize_r10_not_mangled_to_r1(self):
        assert AnchorageGISPlugin._normalize_zoning_district("R10SL") == (
            "R10",
            "bowl",
        )

    def test_cap_table_spot_values(self):
        caps = AnchorageGISPlugin.LOT_COVERAGE_CAPS
        assert caps["R2D"] == 0.40
        assert caps["R3A"] == 0.50
        assert caps["R3"] == 0.60
        assert caps["R8"] == 0.05
        # Table 21.06-1 sets R-3 / R-4 caps per use: 40% for
        # single-family, 60% for townhouse / multifamily / other.
        by_use = AnchorageGISPlugin.LOT_COVERAGE_USE_CAPS
        assert by_use["R3"]["low"] == 0.40
        assert by_use["R4"]["low"] == 0.40
        assert "Residential 2 Family" in by_use["R3"]["low_land_uses"]
        assert "Residential 2 Family" not in by_use["R4"]["low_land_uses"]
        # Note 3 is attached to the R-1 and R-1A rows only.
        assert AnchorageGISPlugin.LOT_COVERAGE_NOTE3_DISTRICTS == {
            "R1",
            "R1A",
        }
        assert "R4A" not in caps
        assert "R4A" in AnchorageGISPlugin.LOT_COVERAGE_UNRESTRICTED

    # -- clip math (pure geometry, no mocks) --

    def test_clip_building_half_inside(self):
        parcel = AnchorageGISPlugin._esri_rings_to_clipper_paths(
            _FP_SQUARE_RINGS
        )
        bldg = AnchorageGISPlugin._geojson_to_clipper_paths(
            {
                "type": "Polygon",
                "coordinates": [
                    [[-5, 0], [5, 0], [5, 10], [-5, 10], [-5, 0]]
                ],
            }
        )
        area = AnchorageGISPlugin._clip_intersection_area_m2(bldg, parcel)
        assert area == pytest.approx(50.0, rel=1e-6)

    def test_clip_building_with_hole(self):
        parcel = AnchorageGISPlugin._esri_rings_to_clipper_paths(
            _FP_SQUARE_RINGS
        )
        bldg = AnchorageGISPlugin._geojson_to_clipper_paths(
            {
                "type": "Polygon",
                "coordinates": [
                    [[2, 2], [12, 2], [12, 12], [2, 12], [2, 2]],
                    [[5, 5], [7, 5], [7, 7], [5, 7], [5, 5]],
                ],
            }
        )
        area = AnchorageGISPlugin._clip_intersection_area_m2(bldg, parcel)
        assert area == pytest.approx(96.0, rel=1e-6)

    def test_clip_overlapping_buildings_not_double_counted(self):
        parcel = AnchorageGISPlugin._esri_rings_to_clipper_paths(
            _FP_SQUARE_RINGS
        )
        square = {
            "type": "Polygon",
            "coordinates": [[[2, 2], [12, 2], [12, 12], [2, 12], [2, 2]]],
        }
        paths = AnchorageGISPlugin._geojson_to_clipper_paths(
            square
        ) + AnchorageGISPlugin._geojson_to_clipper_paths(square)
        area = AnchorageGISPlugin._clip_intersection_area_m2(paths, parcel)
        assert area == pytest.approx(100.0, rel=1e-6)

    def test_clip_building_outside_parcel_is_zero(self):
        parcel = AnchorageGISPlugin._esri_rings_to_clipper_paths(
            _FP_SQUARE_RINGS
        )
        bldg = AnchorageGISPlugin._geojson_to_clipper_paths(
            {
                "type": "Polygon",
                "coordinates": [
                    [[30, 30], [40, 30], [40, 40], [30, 40], [30, 30]]
                ],
            }
        )
        assert (
            AnchorageGISPlugin._clip_intersection_area_m2(bldg, parcel)
            == 0.0
        )

    # -- full flow (mocked upstream) --

    @pytest.mark.asyncio
    async def test_happy_path_numbers(self, plugin):
        # 10x10m building fully inside the 20x20m lot: 100 m2 =
        # 1,076.39 sqft on a 4,306 sqft lot -> 25.0% coverage, R1 cap
        # 40% -> max 1,722 sqft, headroom ~646 sqft; note 3 applies.
        text = await _fp_run(
            plugin,
            [
                {
                    "attributes": _fp_parcel_attrs(Zoning_District="R1"),
                    "geometry": {"rings": _FP_SQUARE_RINGS},
                }
            ],
            [
                _fp_building(
                    [[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]]
                )
            ],
        )
        assert '"existing_footprint_sqft": 1076' in text
        assert '"coverage_pct": 0.25' in text
        assert '"district_max_coverage": 0.4' in text
        assert '"max_footprint_sqft": 1722' in text
        assert '"adu_footprint_headroom_sqft": 646' in text
        assert '"building_count": 1' in text
        # note 3: 50% of 4,306 = 2,153 - 1,076 = 1,077
        assert '"headroom_if_note3_50pct": 1077' in text
        assert "Shape__Area" in text  # caveat present

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "zone,land_use,cap",
        [
            ("R3", "Residential 1 Family", 0.4),
            ("R3", "Residential 2 Family", 0.4),
            ("R3", "Apartments - Garden", 0.6),
            ("R3", "Residential Vacant Land", 0.4),
            ("R3", None, 0.4),
            ("R4", "Residential 1 Family", 0.4),
            ("R4", "Residential 2 Family", 0.6),
            ("R4", "Fourplex", 0.6),
            ("R2M", "Residential 1 Family", 0.4),
            ("R3A", "Apartments - Garden", 0.5),
        ],
    )
    async def test_cap_follows_the_dwelling_type(
        self, plugin, zone, land_use, cap
    ):
        text = await _fp_run(
            plugin,
            [
                {
                    "attributes": _fp_parcel_attrs(
                        Zoning_District=zone, Land_Use=land_use
                    ),
                    "geometry": {"rings": _FP_SQUARE_RINGS},
                }
            ],
            [
                _fp_building(
                    [[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]]
                )
            ],
        )
        assert f'"district_max_coverage": {cap}' in text
        # Both figures are always named, so a reader can see the cap
        # is conditional whichever one was applied.
        assert "depends on the dwelling type" in text
        assert "townhouse" in text
        assert "headroom_if_note3_50pct" not in text

    @pytest.mark.asyncio
    async def test_note3_is_not_offered_in_r2_districts(self, plugin):
        text = await _fp_run(
            plugin,
            [
                {
                    "attributes": _fp_parcel_attrs(Zoning_District="R2D"),
                    "geometry": {"rings": _FP_SQUARE_RINGS},
                }
            ],
            [
                _fp_building(
                    [[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]]
                )
            ],
        )
        assert '"district_max_coverage": 0.4' in text
        assert "note3" not in text
        assert "note 3" not in text

    @pytest.mark.asyncio
    async def test_shared_building_clipped_to_lot(self, plugin):
        # Attached-housing shape: the building polygon extends far
        # beyond the lot; only the on-lot part (10x20=200 m2) counts.
        text = await _fp_run(
            plugin,
            [
                {
                    "attributes": _fp_parcel_attrs(),
                    "geometry": {"rings": _FP_SQUARE_RINGS},
                }
            ],
            [
                _fp_building(
                    [[[10, -50], [80, -50], [80, 70], [10, 70], [10, -50]]]
                )
            ],
        )
        # 200 m2 = 2,152.78 sqft, NOT the whole 8,400 m2 building.
        assert '"existing_footprint_sqft": 2153' in text
        assert '"coverage_pct": 0.5' in text

    @pytest.mark.asyncio
    async def test_not_found(self, plugin):
        text = await _fp_run(plugin, [], [], parcel_id="999-999-99")
        assert "no parcel found" in text
        assert "find_parcel" in text

    @pytest.mark.asyncio
    async def test_multiple_distinct_parcels_listed_not_computed(
        self, plugin
    ):
        feats = [
            {
                "attributes": _fp_parcel_attrs(
                    Parcel_ID=f"0032647700{i}", Condo_Unit_Number=str(i)
                ),
                "geometry": {"rings": _FP_SQUARE_RINGS},
            }
            for i in (1, 2)
        ]
        text = await _fp_run(plugin, feats, [])
        assert "matched 2 parcel records" in text
        assert "coverage_pct" not in text

    @pytest.mark.asyncio
    async def test_no_lot_size_clean_response(self, plugin):
        feats = [
            {
                "attributes": _fp_parcel_attrs(
                    Lot_Size=0, Condo_Unit_Number="4B"
                ),
                "geometry": {"rings": _FP_SQUARE_RINGS},
            }
        ]
        text = await _fp_run(plugin, feats, [])
        assert "no independent lot" in text
        assert "coverage_pct" not in text

    @pytest.mark.asyncio
    async def test_ce_district_cap_not_assumed(self, plugin):
        feats = [
            {
                "attributes": _fp_parcel_attrs(Zoning_District="CER2M"),
                "geometry": {"rings": _FP_SQUARE_RINGS},
            }
        ]
        text = await _fp_run(
            plugin,
            feats,
            [_fp_building([[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]])],
        )
        assert '"district_max_coverage": null' in text
        assert "21.10" in text
        assert '"coverage_pct": 0.25' in text  # coverage still computed

    @pytest.mark.asyncio
    async def test_r4a_unrestricted_skips_headroom(self, plugin):
        feats = [
            {
                "attributes": _fp_parcel_attrs(Zoning_District="R4A"),
                "geometry": {"rings": _FP_SQUARE_RINGS},
            }
        ]
        text = await _fp_run(
            plugin,
            feats,
            [_fp_building([[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]])],
        )
        assert '"district_max_coverage": null' in text
        assert '"adu_footprint_headroom_sqft": null' in text
        assert "unrestricted" in text

    @pytest.mark.asyncio
    async def test_missing_parcel_id_errors(self, plugin):
        result = await plugin.execute_tool("footprint_for_parcel", {})
        assert result.success is False
        assert "parcel_id is required" in result.error_message

    @pytest.mark.asyncio
    async def test_lot_size_geometry_mismatch_caveat(self, plugin):
        feats = [
            {
                # 20x20m = ~4,306 sqft mapped, assessor says 10,000.
                "attributes": _fp_parcel_attrs(Lot_Size=10000),
                "geometry": {"rings": _FP_SQUARE_RINGS},
            }
        ]
        text = await _fp_run(
            plugin,
            feats,
            [_fp_building([[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]])],
        )
        assert ">15% apart" in text


# ── municipal coverage box ─────────────────────────────────────────────


class TestMunicipalCoverageBox:
    """The LIMITED COVERAGE caveat must not fire on a muni-wide layer."""

    def test_parcel_layer_extent_reads_as_full_coverage(self):
        # PropertyInformation_Hosted's extent, measured 2026-11-18. The
        # looser box this replaced scored it 48%, under the 50%
        # threshold, so every parcel query carried a false warning.
        pct = AnchorageGISPlugin._region_coverage_pct(
            {
                "xmin": -150.288,
                "ymin": 60.768,
                "xmax": -148.802,
                "ymax": 61.486,
                "spatialReference": {"wkid": 4326},
            }
        )
        assert pct > 0.9
        assert pct >= AnchorageGISPlugin.COVERAGE_THRESHOLD

    def test_a_single_neighbourhood_is_still_flagged(self):
        pct = AnchorageGISPlugin._region_coverage_pct(
            {
                "xmin": -149.90,
                "ymin": 61.20,
                "xmax": -149.85,
                "ymax": 61.22,
                "spatialReference": {"wkid": 4326},
            }
        )
        assert pct < AnchorageGISPlugin.COVERAGE_THRESHOLD


# ── find_parcel: what a match reports ──────────────────────────────────


class TestFindParcelMatchReporting:
    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @staticmethod
    async def _run(plugin, features, args, flagged=False):
        seen = {}

        async def fake_get(url, params=None, **kwargs):
            seen.update(params or {})
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            body = {"features": features}
            if flagged:
                body["exceededTransferLimit"] = True
            resp.json.return_value = body
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get
            base = {
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "003-184-87",
            }
            base.update(args)
            text, structured = await plugin._find_parcel(base)
        return text, structured, seen

    @pytest.mark.asyncio
    async def test_matched_field_is_fetched_when_out_fields_omits_it(
        self, plugin
    ):
        """canonical=null means "nothing matched"; a narrowed out_fields
        used to produce it on a successful match."""
        feats = [{"attributes": {"Parcel_Num": "00318487000", "Owner": "X"}}]
        _, s, sent = await self._run(plugin, feats, {"out_fields": "Owner"})
        assert sent["outFields"] == "Owner,Parcel_Num"
        assert s["summary"]["canonical"] == "00318487000"
        assert s["summary"]["matched_values"] == ["00318487000"]

    @pytest.mark.asyncio
    async def test_a_full_page_is_flagged_as_possibly_partial(self, plugin):
        """Every unit of a condo complex shares the master number."""
        feats = [
            {"attributes": {"Parcel_Num": "00318487000", "Unit": i}}
            for i in range(10)
        ]
        text, s, _ = await self._run(plugin, feats, {})
        assert [c["code"] for c in s["caveats"]] == ["limit_reached"]
        assert "10 record(s) returned" in text
        assert "more records may share this parcel number" in text

    @pytest.mark.asyncio
    async def test_an_ordinary_match_carries_no_caveat(self, plugin):
        feats = [{"attributes": {"Parcel_Num": "00318487000"}}]
        text, s, _ = await self._run(plugin, feats, {})
        assert s["caveats"] == []
        assert "1 record(s) found" in text
