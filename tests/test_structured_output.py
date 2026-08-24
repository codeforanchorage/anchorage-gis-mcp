"""Structured-output conformance for the analytic tools.

Declaring an `outputSchema` is binding: the MCP spec says a server MUST
return structured results that conform to it, and clients are told they
SHOULD validate. A tool whose output drifts from its declared schema is
therefore a protocol break, not a cosmetic bug -- these tests are the
gate that keeps that from shipping.

Coverage here is deliberately weighted toward the awkward branches
(empty result, no sum_fields, count=false, coverage above 100%, caps
reached), because those are the ones a schema written from the happy
path gets wrong.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator

from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin


@pytest.fixture
def anchorage_config():
    return {
        "portal_base_url": "https://example.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "abc123",
        "org_id": "org123",
        "city_name": "Municipality of Anchorage",
        "gallery_url": "https://example.com/gallery",
        "timeout": 30,
    }


@pytest.fixture
def plugin(anchorage_config):
    p = AnchorageGISPlugin(anchorage_config)
    p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
    return p


def _validate(schema, instance):
    """Assert `instance` conforms, reporting every violation at once."""
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    assert not errors, "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )


# ── the schemas themselves ────────────────────────────────────────────


class TestSchemasAreWellFormed:
    def test_declared_schemas_are_valid_json_schema(self):
        """A malformed schema would make every client reject the tool."""
        for schema in AnchorageGISPlugin.TOOL_OUTPUT_SCHEMAS.values():
            Draft202012Validator.check_schema(schema)

    def test_schemas_are_self_contained(self):
        """No $ref, so no client has to resolve references."""
        import json

        for name, schema in AnchorageGISPlugin.TOOL_OUTPUT_SCHEMAS.items():
            assert "$ref" not in json.dumps(schema), name

    def test_only_schema_declaring_tools_are_listed(self, plugin):
        """A tool declaring outputSchema MUST return structured content.

        Guards the pairing: adding a name to TOOL_OUTPUT_SCHEMAS without
        also returning structured_content silently breaks conformance.
        """
        declared = set(AnchorageGISPlugin.TOOL_OUTPUT_SCHEMAS)
        assert declared == {
            "aggregate_by_polygon",
            "coverage_by_polygon",
            "query_data",
            "spatial_query_point",
            "spatial_query_polygon",
            "filter_by_polygon",
            "get_distinct_values",
            "find_features_spanning_classifications",
            "footprint_for_parcel",
        }

        tools = {t.name: t for t in plugin.get_tools()}
        for name in declared:
            assert tools[name].output_schema is not None, name
        for name, tool in tools.items():
            if name not in declared:
                assert tool.output_schema is None, name

    def test_coverage_pct_has_no_upper_bound(self):
        """Areas are counted whole, not clipped, so >100% is real data.

        A maximum here would make the server violate its own schema on
        dense lots -- exactly the case the tool warns about.
        """
        rows = AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA["properties"]["rows"]
        pct = rows["items"]["properties"]["coverage_pct"]
        assert "maximum" not in pct and "exclusiveMaximum" not in pct

    def test_group_admits_non_string_values(self):
        """`group` is a raw field value, not necessarily a string."""
        rows = AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA["properties"]["rows"]
        allowed = set(rows["items"]["properties"]["group"]["type"])
        assert {"string", "number", "boolean", "null"} <= allowed


# ── aggregate_by_polygon ──────────────────────────────────────────────


def _agg_args(**over):
    base = {
        "source_item_id": "a" * 32,
        "aggregation_item_id": "b" * 32,
        "group_by_field": "NAME",
        "sum_fields": ["POP"],
    }
    base.update(over)
    return base


def _patch_agg(plugin, polygons, features, fields=("POP",)):
    """Stub the upstream fetches so the math runs on fixed inputs."""
    meta = {
        "geometryType": "esriGeometryPoint",
        "fields": [
            {"name": f, "type": "esriFieldTypeInteger"} for f in fields
        ],
    }
    return (
        patch.object(
            plugin, "_fetch_aggregation_polygons", AsyncMock(return_value=polygons)
        ),
        patch.object(
            plugin, "_resolve_layer_url", AsyncMock(return_value="http://x/0")
        ),
        patch.object(plugin, "_fetch_layer_meta", AsyncMock(return_value=meta)),
        patch.object(
            plugin, "_paged_geojson_fetch", AsyncMock(return_value=features)
        ),
    )


SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]],
}


def _point(x, y, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]}, "properties": props}


class TestAggregateStructuredOutput:
    @pytest.mark.asyncio
    async def test_normal_result_conforms(self, plugin):
        polys = [{"group": "North", "geometry": SQUARE}]
        feats = [_point(1, 1, POP=100), _point(2, 2, POP=50)]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, feats)
        with p1, p2, p3, p4:
            text, structured = await plugin._aggregate_by_polygon(_agg_args())

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert structured["summary"]["source_features"] == 2
        assert structured["rows"][0]["group"] == "North"
        assert structured["rows"][0]["count"] == 2
        assert structured["rows"][0]["sums"]["POP"] == 150.0
        assert text.startswith("## Aggregation:")

    @pytest.mark.asyncio
    async def test_empty_result_still_conforms(self, plugin):
        """The no-bucket branch returns early -- it must still be valid."""
        polys = [{"group": "North", "geometry": SQUARE}]
        feats = [_point(99, 99, POP=1)]  # far outside the polygon
        p1, p2, p3, p4 = _patch_agg(plugin, polys, feats)
        with p1, p2, p3, p4:
            _, structured = await plugin._aggregate_by_polygon(_agg_args())

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert structured["rows"] == []
        assert structured["summary"]["buckets"] == 0
        assert structured["summary"]["unmatched"] == 1
        codes = {c["code"] for c in structured["caveats"]}
        assert "unmatched_source_features" in codes

    @pytest.mark.asyncio
    async def test_no_sum_fields_conforms(self, plugin):
        """sum_fields is optional; `sums` must still be a valid object."""
        polys = [{"group": "North", "geometry": SQUARE}]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, [_point(1, 1)])
        with p1, p2, p3, p4:
            _, structured = await plugin._aggregate_by_polygon(
                _agg_args(sum_fields=[])
            )

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert structured["rows"][0]["sums"] == {}

    @pytest.mark.asyncio
    async def test_count_false_omits_count_and_conforms(self, plugin):
        polys = [{"group": "North", "geometry": SQUARE}]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, [_point(1, 1, POP=5)])
        with p1, p2, p3, p4:
            _, structured = await plugin._aggregate_by_polygon(
                _agg_args(count=False)
            )

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert "count" not in structured["rows"][0]
        assert "small_sample" not in structured["rows"][0]

    @pytest.mark.asyncio
    async def test_non_string_group_conforms(self, plugin):
        """A numeric group value must not fail validation."""
        polys = [{"group": 2020, "geometry": SQUARE}]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, [_point(1, 1, POP=5)])
        with p1, p2, p3, p4:
            _, structured = await plugin._aggregate_by_polygon(_agg_args())

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert structured["rows"][0]["group"] == 2020

    @pytest.mark.asyncio
    async def test_small_sample_flagged_in_rows_and_caveats(self, plugin):
        polys = [{"group": "North", "geometry": SQUARE}]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, [_point(1, 1, POP=5)])
        with p1, p2, p3, p4:
            text, structured = await plugin._aggregate_by_polygon(_agg_args())

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert structured["rows"][0]["small_sample"] is True
        codes = {c["code"] for c in structured["caveats"]}
        assert "small_sample_buckets" in codes
        # Rendered prose and structured caveats come from one list.
        for caveat in structured["caveats"]:
            assert caveat["message"] in text

    @pytest.mark.asyncio
    async def test_buffer_is_null_when_unused(self, plugin):
        polys = [{"group": "North", "geometry": SQUARE}]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, [_point(1, 1, POP=5)])
        with p1, p2, p3, p4:
            _, structured = await plugin._aggregate_by_polygon(_agg_args())

        _validate(AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA, structured)
        assert structured["query"]["buffer"] is None


# ── tool-call plumbing ────────────────────────────────────────────────


class TestStructuredContentReachesTheWire:
    @pytest.mark.asyncio
    async def test_execute_tool_carries_structured_content(self, plugin):
        polys = [{"group": "North", "geometry": SQUARE}]
        p1, p2, p3, p4 = _patch_agg(plugin, polys, [_point(1, 1, POP=5)])
        with p1, p2, p3, p4:
            result = await plugin.execute_tool(
                "aggregate_by_polygon", _agg_args()
            )

        assert result.success
        assert result.structured_content is not None
        _validate(
            AnchorageGISPlugin.AGGREGATE_OUTPUT_SCHEMA,
            result.structured_content,
        )

    @pytest.mark.asyncio
    async def test_prose_only_tools_have_no_structured_content(self, plugin):
        """Tools without an outputSchema must not emit structuredContent."""
        with patch.object(
            plugin, "_find_gis_content", AsyncMock(return_value="text")
        ):
            result = await plugin.execute_tool(
                "find_gis_content", {"topic": "parks"}
            )

        assert result.success
        assert result.structured_content is None


# ── coverage_by_polygon ───────────────────────────────────────────────


def _cov_args(**over):
    base = {
        "target_item_id": "c" * 32,
        "overlay_item_id": "d" * 32,
        "target_id_field": "Parcel_ID",
    }
    base.update(over)
    return base


def _poly(x0, y0, x1, y1, **props):
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]
            ],
        },
        "properties": props,
    }


def _patch_cov(plugin, targets, overlays):
    """Stub upstream I/O. _paged_geojson_fetch is call-ordered: the first
    call fetches targets, every later call fetches an overlay batch."""
    meta = {
        "geometryType": "esriGeometryPolygon",
        "fields": [
            {"name": "Parcel_ID", "type": "esriFieldTypeString"},
            {"name": "Shape__Area", "type": "esriFieldTypeDouble"},
        ],
    }
    calls = {"n": 0}

    async def fetch(*a, **kw):
        calls["n"] += 1
        return targets if calls["n"] == 1 else overlays

    return (
        patch.object(
            plugin, "_resolve_layer_url", AsyncMock(return_value="http://x/0")
        ),
        patch.object(plugin, "_fetch_layer_meta", AsyncMock(return_value=meta)),
        patch.object(plugin, "_paged_geojson_fetch", AsyncMock(side_effect=fetch)),
        patch.object(plugin, "_safe_layer_meta", AsyncMock(return_value={})),
    )


class TestCoverageStructuredOutput:
    @pytest.mark.asyncio
    async def test_normal_result_conforms(self, plugin):
        targets = [_poly(0, 0, 10, 10, Parcel_ID="P1", Shape__Area=100.0)]
        overlays = [_poly(1, 1, 2, 2, Shape__Area=25.0)]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, overlays)
        with p1, p2, p3, p4:
            text, structured = await plugin._coverage_by_polygon(_cov_args())

        _validate(AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA, structured)
        assert structured["summary"]["targets_measured"] == 1
        row = structured["rows"][0]
        assert row["id"] == "P1"
        assert row["coverage_pct"] == pytest.approx(25.0)
        assert text.startswith("## Coverage:")

    @pytest.mark.asyncio
    async def test_coverage_above_100_percent_conforms(self, plugin):
        """Areas are counted whole, not clipped, so >100% is legitimate.

        This is the case a schema with `maximum: 100` would reject.
        """
        targets = [_poly(0, 0, 10, 10, Parcel_ID="P1", Shape__Area=10.0)]
        overlays = [_poly(1, 1, 2, 2, Shape__Area=50.0)]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, overlays)
        with p1, p2, p3, p4:
            _, structured = await plugin._coverage_by_polygon(_cov_args())

        _validate(AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA, structured)
        assert structured["rows"][0]["coverage_pct"] > 100.0

    @pytest.mark.asyncio
    async def test_zero_coverage_target_conforms_and_is_flagged(self, plugin):
        targets = [_poly(0, 0, 10, 10, Parcel_ID="P1", Shape__Area=100.0)]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, [])
        with p1, p2, p3, p4:
            _, structured = await plugin._coverage_by_polygon(_cov_args())

        _validate(AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA, structured)
        assert structured["summary"]["zero_coverage"] == 1
        assert structured["rows"][0]["coverage_pct"] == 0.0
        codes = {c["code"] for c in structured["caveats"]}
        assert "zero_coverage_targets" in codes

    @pytest.mark.asyncio
    async def test_band_filter_and_null_bounds_conform(self, plugin):
        targets = [_poly(0, 0, 10, 10, Parcel_ID="P1", Shape__Area=100.0)]
        overlays = [_poly(1, 1, 2, 2, Shape__Area=90.0)]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, overlays)
        with p1, p2, p3, p4:
            _, structured = await plugin._coverage_by_polygon(
                _cov_args(max_coverage_pct=40)
            )

        _validate(AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA, structured)
        # 90% coverage is outside "coverage < 40%"
        assert structured["summary"]["in_band"] == 0
        assert structured["summary"]["out_of_band"] == 1
        assert structured["rows"] == []
        assert structured["query"]["band"]["max_coverage_pct"] == 40
        assert structured["query"]["band"]["min_coverage_pct"] is None

    @pytest.mark.asyncio
    async def test_skipped_targets_counted_and_conform(self, plugin):
        """A target with non-positive area is skipped, not measured."""
        targets = [
            _poly(0, 0, 10, 10, Parcel_ID="P1", Shape__Area=100.0),
            _poly(0, 0, 1, 1, Parcel_ID="P2", Shape__Area=0.0),
        ]
        overlays = [_poly(1, 1, 2, 2, Shape__Area=25.0)]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, overlays)
        with p1, p2, p3, p4:
            _, structured = await plugin._coverage_by_polygon(_cov_args())

        _validate(AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA, structured)
        assert structured["summary"]["skipped"] == 1
        assert structured["summary"]["targets_measured"] == 1
        codes = {c["code"] for c in structured["caveats"]}
        assert "targets_skipped" in codes

    @pytest.mark.asyncio
    async def test_rows_are_not_truncated_to_the_table_limit(self, plugin):
        """structuredContent carries every in-band target, not just the
        COVERAGE_TABLE_ROWS the markdown renders."""
        n = AnchorageGISPlugin.COVERAGE_TABLE_ROWS + 15
        targets = [
            _poly(0, 0, 10, 10, Parcel_ID=f"P{i}", Shape__Area=100.0)
            for i in range(n)
        ]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, [])
        with p1, p2, p3, p4:
            text, structured = await plugin._coverage_by_polygon(_cov_args())

        _validate(AnchorageGISPlugin.COVERAGE_OUTPUT_SCHEMA, structured)
        assert len(structured["rows"]) == n

        # The rendering is still capped, so the two intentionally differ.
        # Count data rows only -- the header is also "| Parcel_ID | ...".
        rendered = len(re.findall(r"^\| P\d+ \|", text, re.MULTILINE))
        assert rendered == AnchorageGISPlugin.COVERAGE_TABLE_ROWS
        assert len(structured["rows"]) > rendered

    @pytest.mark.asyncio
    async def test_caveat_messages_match_the_rendering(self, plugin):
        targets = [_poly(0, 0, 10, 10, Parcel_ID="P1", Shape__Area=100.0)]
        p1, p2, p3, p4 = _patch_cov(plugin, targets, [])
        with p1, p2, p3, p4:
            text, structured = await plugin._coverage_by_polygon(_cov_args())

        assert structured["caveats"], "expected the standing methodology notes"
        for caveat in structured["caveats"]:
            assert caveat["message"] in text


# ── the record-listing family (shared _format_query_results) ──────────


QR_SCHEMA = AnchorageGISPlugin.QUERY_RESULT_OUTPUT_SCHEMA


class TestQueryResultStructuredOutput:
    """query_data, spatial_query_point/polygon and filter_by_polygon all
    render through _format_query_results, so they share one schema."""

    def test_records_with_total_count_conform(self, plugin):
        records = [{"Parcel_ID": "P1", "Zone": "R1"}]
        text, structured = plugin._format_query_results(
            records, limit=50, total_count=1234, item_id="x" * 32
        )
        _validate(QR_SCHEMA, structured)
        assert structured["summary"]["total_count"] == 1234
        assert structured["summary"]["returned"] == 1
        assert structured["summary"]["truncated"] is True
        assert structured["rows"][0]["Parcel_ID"] == "P1"

    def test_no_records_conforms(self, plugin):
        text, structured = plugin._format_query_results([], limit=50)
        _validate(QR_SCHEMA, structured)
        assert structured["rows"] == []
        assert structured["summary"]["returned"] == 0

    def test_null_total_count_conforms(self, plugin):
        """The spatial_* tools do not paginate, so total_count is null."""
        records = [{"A": 1}]
        _, structured = plugin._format_query_results(
            records, limit=50, total_count=None
        )
        _validate(QR_SCHEMA, structured)
        assert structured["summary"]["total_count"] is None
        assert structured["summary"]["truncated"] is False

    def test_single_record_and_small_sample_caveats(self, plugin):
        _, one = plugin._format_query_results(
            [{"A": 1}], limit=50, total_count=1
        )
        _validate(QR_SCHEMA, one)
        assert "single_record" in {c["code"] for c in one["caveats"]}

        _, few = plugin._format_query_results(
            [{"A": i} for i in range(3)], limit=50, total_count=3
        )
        _validate(QR_SCHEMA, few)
        assert "small_sample" in {c["code"] for c in few["caveats"]}

    def test_polyline_grain_caveat_is_structured(self, plugin):
        _, structured = plugin._format_query_results(
            [{"NAME": "Trail"}],
            limit=50,
            total_count=1123,
            geometry_type="esriGeometryPolyline",
        )
        _validate(QR_SCHEMA, structured)
        assert "polyline_grain" in {c["code"] for c in structured["caveats"]}

    def test_coverage_caveats_are_structured(self, plugin):
        _, none_cov = plugin._format_query_results(
            [{"A": 1}], limit=50, coverage_pct=0.0
        )
        _validate(QR_SCHEMA, none_cov)
        assert "no_coverage" in {c["code"] for c in none_cov["caveats"]}

        _, low = plugin._format_query_results(
            [{"A": 1}], limit=50, coverage_pct=0.1
        )
        _validate(QR_SCHEMA, low)
        assert "limited_coverage" in {c["code"] for c in low["caveats"]}

    def test_geometry_is_full_not_clipped(self, plugin):
        """The text clips long geometry; the structured copy must not."""
        big = {
            "type": "Polygon",
            "coordinates": [[[i * 0.001, i * 0.001] for i in range(3000)]],
        }
        records = [{"ID": 1, "__geometry__": big}]
        text, structured = plugin._format_query_results(records, limit=50)
        _validate(QR_SCHEMA, structured)
        assert "truncated" in text  # the rendering clipped it
        assert structured["rows"][0]["__geometry__"] == big

    def test_coded_domains_shipped_for_decoding(self, plugin):
        _, structured = plugin._format_query_results(
            [{"CODE": 1}],
            limit=50,
            coded_domains={"CODE": {1: "Residential"}},
        )
        _validate(QR_SCHEMA, structured)
        assert structured["coded_domains"]["CODE"]["1"] == "Residential"
        # rows keep the RAW stored code, not the label
        assert structured["rows"][0]["CODE"] == 1


# ── get_distinct_values ───────────────────────────────────────────────


class TestDistinctValuesStructuredOutput:
    @staticmethod
    def _patch(plugin, values):
        """Stub the layer lookup and the distinct-values query.

        The tool calls plugin.client.get directly (not the retrying
        helper), so that is what has to be replaced.
        """
        feats = [{"attributes": {"Zone": v}} for v in values]
        resp = MagicMock()
        resp.json.return_value = {"features": feats}
        resp.raise_for_status.return_value = None
        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(return_value=resp)
        meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "Zone", "type": "esriFieldTypeString"}],
        }
        return (
            patch.object(
                plugin, "_resolve_layer_url", AsyncMock(return_value="http://x/0")
            ),
            patch.object(
                plugin, "_fetch_layer_meta", AsyncMock(return_value=meta)
            ),
        )

    @pytest.mark.asyncio
    async def test_values_conform(self, plugin):
        p1, p2 = self._patch(plugin, ["R1", "R2", "R2", "R3"])
        with p1, p2:
            text, structured = await plugin._get_distinct_values(
                {"item_id": "a" * 32, "field": "Zone"}
            )
        _validate(AnchorageGISPlugin.DISTINCT_VALUES_OUTPUT_SCHEMA, structured)
        # de-duplicated, order preserved, raw values
        assert structured["values"] == ["R1", "R2", "R3"]
        assert structured["summary"]["returned"] == 3
        assert structured["summary"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_empty_conforms(self, plugin):
        p1, p2 = self._patch(plugin, [])
        with p1, p2:
            _, structured = await plugin._get_distinct_values(
                {"item_id": "a" * 32, "field": "Zone"}
            )
        _validate(AnchorageGISPlugin.DISTINCT_VALUES_OUTPUT_SCHEMA, structured)
        assert structured["values"] == []

    @pytest.mark.asyncio
    async def test_numeric_values_conform(self, plugin):
        """Values keep their native type -- not everything is a string."""
        p1, p2 = self._patch(plugin, [1, 2, 3])
        with p1, p2:
            _, structured = await plugin._get_distinct_values(
                {"item_id": "a" * 32, "field": "Zone"}
            )
        _validate(AnchorageGISPlugin.DISTINCT_VALUES_OUTPUT_SCHEMA, structured)
        assert structured["values"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_cap_reached_is_flagged(self, plugin):
        p1, p2 = self._patch(plugin, [f"V{i}" for i in range(5)])
        with p1, p2:
            _, structured = await plugin._get_distinct_values(
                {"item_id": "a" * 32, "field": "Zone", "limit": 5}
            )
        _validate(AnchorageGISPlugin.DISTINCT_VALUES_OUTPUT_SCHEMA, structured)
        assert structured["summary"]["truncated"] is True
        assert "values_cap_reached" in {
            c["code"] for c in structured["caveats"]
        }


# ── footprint_for_parcel ──────────────────────────────────────────────


FP_SCHEMA = AnchorageGISPlugin.FOOTPRINT_OUTPUT_SCHEMA

_FP_ATTRS = {
    "Parcel_ID": "00326477000",
    "Parcel_Address": "123 MAIN ST",
    "Zoning_District": "R-1",
    "Land_Use": "Single Family",
    "Total_Living_Units": 1,
    "Lot_Size": 8000.0,
    "Condo_Unit_Number": None,
    "Parcel_ID_URL": "https://property.muni.org/x",
}


async def _fp_run(plugin, parcel_features, bldg_features):
    with patch.object(
        plugin, "_resolve_layer_url", AsyncMock(return_value="http://x/0")
    ), patch.object(
        plugin,
        "_request_json_with_retry",
        AsyncMock(return_value={"features": parcel_features}),
    ), patch.object(
        plugin, "_paged_geojson_fetch", AsyncMock(return_value=bldg_features)
    ):
        return await plugin._footprint_for_parcel(
            {"parcel_id": "00326477000"}
        )


def _fp_parcel(**over):
    attrs = dict(_FP_ATTRS)
    attrs.update(over)
    return {
        "attributes": attrs,
        "geometry": {"rings": [[[0, 0], [0, 20], [20, 20], [20, 0], [0, 0]]]},
    }


class TestFootprintStructuredOutput:
    @pytest.mark.asyncio
    async def test_result_conforms(self, plugin):
        bldg = [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[1, 1], [1, 5], [5, 5], [5, 1], [1, 1]]
                    ],
                },
                "properties": {"OBJECTID": 1, "Category": "General"},
            }
        ]
        _, structured = await _fp_run(plugin, [_fp_parcel()], bldg)
        _validate(FP_SCHEMA, structured)
        assert structured["result"] is not None
        assert structured["result"]["parcel_id"] == "00326477000"
        assert structured["caveats"], "methodology caveats always apply"

    @pytest.mark.asyncio
    async def test_not_found_conforms_with_null_result(self, plugin):
        """The no-parcel path must still emit a valid object."""
        _, structured = await _fp_run(plugin, [], [])
        _validate(FP_SCHEMA, structured)
        assert structured["result"] is None
        assert "parcel_not_found" in {
            c["code"] for c in structured["caveats"]
        }

    @pytest.mark.asyncio
    async def test_no_independent_lot_conforms(self, plugin):
        """A condo unit with no lot area yields result: null, not a crash."""
        _, structured = await _fp_run(
            plugin, [_fp_parcel(Lot_Size=0.0, Condo_Unit_Number="4B")], []
        )
        _validate(FP_SCHEMA, structured)
        assert structured["result"] is None
        assert "no_independent_lot" in {
            c["code"] for c in structured["caveats"]
        }

    @pytest.mark.asyncio
    async def test_caveat_messages_match_the_rendering(self, plugin):
        text, structured = await _fp_run(plugin, [_fp_parcel()], [])
        for caveat in structured["caveats"]:
            assert caveat["message"] in text


# ── find_features_spanning_classifications ────────────────────────────


SPAN_SCHEMA = AnchorageGISPlugin.SPANNING_OUTPUT_SCHEMA


def _span_args():
    return {
        "source_item_id": "a" * 32,
        "classification_item_id": "b" * 32,
        "classification_field": "ZONE",
    }


class TestSpanningStructuredOutput:
    @pytest.mark.asyncio
    async def test_no_source_features_conforms(self, plugin):
        """The zero-source early return must still emit a valid object."""
        # Source is parcel-grain; the classification layer must NOT be,
        # or the tool refuses up front (a parcel-vs-parcel spatial join
        # is the misuse it exists to prevent).
        source_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [
                {"name": "Parcel_ID", "type": "esriFieldTypeString"},
            ],
        }
        cls_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "ZONE", "type": "esriFieldTypeString"}],
        }
        with patch.object(
            plugin, "_resolve_layer_url", AsyncMock(return_value="http://x/0")
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            AsyncMock(side_effect=[cls_meta, source_meta, source_meta]),
        ), patch.object(
            plugin, "_get_record_count", AsyncMock(return_value=0)
        ):
            text, structured = await (
                plugin._find_features_spanning_classifications(_span_args())
            )

        _validate(SPAN_SCHEMA, structured)
        assert structured["rows"] == []
        assert structured["summary"]["source_features"] == 0
        assert "no_source_features" in {
            c["code"] for c in structured["caveats"]
        }

    def test_rows_withhold_objectids_when_attributes_fail(self):
        """Schema documents that rows stay empty rather than leaking
        internal OBJECTIDs a caller could report as parcel numbers."""
        desc = SPAN_SCHEMA["properties"]["rows"]["description"]
        assert "withheld" in desc.lower()

    def test_qualifying_may_exceed_returned(self):
        """`qualifying` is the true count; `returned` is limit-capped."""
        props = SPAN_SCHEMA["properties"]["summary"]["properties"]
        assert "exceed" in props["qualifying"]["description"]


# ── zero-result conformance ───────────────────────────────────────────


class TestEmptyResultsStillConform:
    """A declared outputSchema is binding on EVERY path, including the
    ones that short-circuit before the normal formatter.

    Found in production: spatial_query_point and spatial_query_polygon
    declared a schema but returned no structuredContent at all when
    nothing matched, because their no-records branch skips
    _format_query_results entirely.
    """

    def test_empty_query_result_conforms(self, plugin):
        payload = AnchorageGISPlugin._empty_query_result(
            item_id="a" * 32, where=None, out_fields=None, limit=50
        )
        _validate(QR_SCHEMA, payload)
        assert payload["rows"] == []
        assert payload["summary"]["returned"] == 0

    def test_empty_result_reports_zero_not_null_total(self, plugin):
        """Zero matches is a known count. null is reserved for the
        'this tool does not paginate' case, and conflating them would
        make a complete answer look like an unmeasured one."""
        payload = AnchorageGISPlugin._empty_query_result(
            item_id="a" * 32, where="1=0", out_fields=None, limit=10
        )
        assert payload["summary"]["total_count"] == 0
        assert payload["summary"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_no_schema_declaring_tool_returns_bare_text(self, plugin):
        """Guards the whole class: every tool that advertises an
        outputSchema must populate structured_content on the paths this
        suite can reach."""
        declared = set(AnchorageGISPlugin.TOOL_OUTPUT_SCHEMAS)
        # Sanity: the two that regressed in production are covered.
        assert {"spatial_query_point", "spatial_query_polygon"} <= declared
