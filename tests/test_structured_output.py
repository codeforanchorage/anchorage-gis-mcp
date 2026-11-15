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


# ── tool-call plumbing ────────────────────────────────────────────────


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


# ── the record-listing family (shared _format_query_results) ──────────


QR_SCHEMA = AnchorageGISPlugin.QUERY_RESULT_OUTPUT_SCHEMA


# ── get_distinct_values ───────────────────────────────────────────────


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


# ── zero-result conformance ───────────────────────────────────────────


# ── discovery / metadata tools ────────────────────────────────────────
#
# These were prose-only until they gained schemas. Structured-only
# clients see nothing else, so beyond conformance the tests pin that the
# judgement calls the prose makes (queryable or not, the ambiguity
# warning, an unconfirmed miss) survive into the structured half.

ITEM_SEARCH = AnchorageGISPlugin.ITEM_SEARCH_OUTPUT_SCHEMA
ITEM_DETAILS = AnchorageGISPlugin.ITEM_DETAILS_OUTPUT_SCHEMA
LAYER_SCHEMA = AnchorageGISPlugin.LAYER_SCHEMA_OUTPUT_SCHEMA
FIELD_SEARCH = AnchorageGISPlugin.FIELD_SEARCH_OUTPUT_SCHEMA
FIND_PARCEL = AnchorageGISPlugin.FIND_PARCEL_OUTPUT_SCHEMA


def _item(item_type="Feature Service", item_id="e" * 32, **over):
    base = {
        "id": item_id,
        "title": f"Item {item_id[:4]}",
        "type": item_type,
        "snippet": "A layer.",
        "tags": ["parks", "recreation"],
        "url": "https://services.arcgis.com/x/arcgis/rest/services/P/"
        "FeatureServer",
    }
    base.update(over)
    return base


def _http(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


LAYER_META = {
    "name": "Parks",
    "geometryType": "esriGeometryPolygon",
    "maxRecordCount": 2000,
    "fields": [
        {
            "name": "OBJECTID",
            "alias": "OBJECTID",
            "type": "esriFieldTypeOID",
        },
        {
            "name": "Park_Name",
            "alias": "Park Name",
            "type": "esriFieldTypeString",
        },
        {
            "name": "Status",
            "type": "esriFieldTypeSmallInteger",
            "domain": {
                "type": "codedValue",
                "name": "StatusDom",
                "codedValues": [
                    {"code": 1, "name": "Open"},
                    {"code": 2, "name": "Closed"},
                ],
            },
        },
    ],
}


class TestFindParcelStructuredOutput:
    def _patch(self, plugin, *responses):
        plugin._resolve_layer_url = AsyncMock(return_value="https://x/0")
        plugin._fetch_layer_meta = AsyncMock(
            return_value={"fields": [{"name": "Parcel_Num"}]}
        )
        plugin._send = AsyncMock(
            side_effect=[_http(r) for r in responses]
        )

    @pytest.mark.asyncio
    async def test_numeric_stored_ids_conform(self, plugin):
        """A layer may store the parcel id as a number, not a string."""
        self._patch(
            plugin,
            {
                "features": [
                    {"attributes": {"Parcel_Num": 121329, "Owner": None}}
                ]
            },
        )
        _, structured = await plugin._find_parcel(
            {
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "001-213-29",
            }
        )
        _validate(FIND_PARCEL, structured)
        assert structured["summary"]["canonical"] == 121329
        assert structured["query"]["variants"]

    @pytest.mark.asyncio
    async def test_miss_without_candidates_conforms(self, plugin):
        self._patch(plugin, {"features": []}, {"features": []})
        _, structured = await plugin._find_parcel(
            {
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "001-213-29",
            }
        )
        _validate(FIND_PARCEL, structured)
        (caveat,) = structured["caveats"]
        assert caveat["code"] == "parcel_not_found"
        assert caveat["count"] == 0
        assert "candidates" not in caveat
        assert "get_distinct_values" in caveat["message"]
