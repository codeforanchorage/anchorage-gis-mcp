"""footprint_for_parcel must measure every piece of a multipart parcel.

A parcel whose land is in several pieces is published as several rows with
the same Parcel_ID, one polygon each. The tool took the first row: for the
Dimond mall (8 polygons) it reported 5.8% coverage, from one building on a
seventh of the land, against the whole parcel's Lot_Size.

Coordinates are EPSG:3338 metres, as the tool requests them.
"""

from unittest.mock import AsyncMock, patch

import jsonschema
import pytest

from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin
from tests.test_anchorage_gis_plugin import _fp_building, _fp_parcel_attrs

SQFT = 10.7639


def _square(x0, y0, side):
    # Esri orientation: clockwise exterior.
    return [[x0, y0], [x0, y0 + side], [x0 + side, y0 + side], [x0 + side, y0], [x0, y0]]


def _row(rings, **attrs):
    return {"attributes": _fp_parcel_attrs(**attrs), "geometry": {"rings": rings}}


def _building(x0, y0, side):
    ring = [[x0, y0], [x0 + side, y0], [x0 + side, y0 + side], [x0, y0 + side], [x0, y0]]
    return _fp_building([ring])


CONFIG = {
    "portal_base_url": "https://muniorg.maps.arcgis.com/sharing/rest",
    "gallery_group_id": "c34ed10758ec4f4eb8aa6826ee5be3ff",
    "org_id": "Ce3DhLRthdwbHlfF",
    "city_name": "Municipality of Anchorage",
    "gallery_url": "https://muniorg.maps.arcgis.com/apps/instant/filtergallery/index.html",
    "timeout": 30,
}


@pytest.fixture
def plugin():
    p = AnchorageGISPlugin(CONFIG)
    p.plugin_config = AnchorageGISPluginConfig(**CONFIG)
    p.client = AsyncMock()
    return p


async def _run(plugin, rows, buildings):
    filters = []

    async def paged(url, where, out_fields, limit, extra_params=None, method="get"):
        filters.append(extra_params)
        return buildings

    with (
        patch.object(plugin, "_resolve_layer_url", AsyncMock(return_value="https://x/FeatureServer/0")),
        patch.object(plugin, "_request_json_with_retry", AsyncMock(return_value={"features": rows})),
        patch.object(plugin, "_paged_geojson_fetch", new=paged),
    ):
        text, structured = await plugin._footprint_for_parcel({"parcel_id": "00326477000"})
    jsonschema.validate(structured, plugin.FOOTPRINT_OUTPUT_SCHEMA)
    return text, structured, filters


def _codes(structured):
    return [c["code"] for c in structured["caveats"]]


class TestEveryPieceOfTheParcelIsTheLot:
    @pytest.mark.asyncio
    async def test_buildings_on_the_second_piece_are_counted(self, plugin):
        # Two 20 m squares, 100 m apart; Lot_Size is their total.
        total_sqft = round(2 * 400 * SQFT)
        rows = [
            _row([_square(0, 0, 20)], Lot_Size=total_sqft),
            _row([_square(100, 0, 20)], Lot_Size=total_sqft),
        ]
        # A 10 m building on EACH piece.
        _, s, filters = await _run(plugin, rows, [_building(5, 5, 10), _building(105, 5, 10)])
        r = s["result"]
        assert r["existing_footprint_sqft"] == round(200 * SQFT)
        assert r["building_count"] == 2
        assert r["coverage_pct"] == 0.25
        assert r["parcel_parts"] == 2
        assert r["coverage_basis"] == "assessor_lot_size"
        assert "multipart_parcel" in _codes(s)
        assert "lot_area_disagreement" not in _codes(s)
        assert "multipart_lot_basis_uncertain" not in _codes(s)
        # Both pieces are in the spatial filter sent upstream.
        assert "100" in filters[0]["geometry"]

    @pytest.mark.asyncio
    async def test_a_single_polygon_parcel_is_unchanged(self, plugin):
        rows = [_row([_square(0, 0, 20)], Lot_Size=4306)]
        _, s, _ = await _run(plugin, rows, [_building(5, 5, 10)])
        r = s["result"]
        assert r["coverage_pct"] == 0.25
        assert "parcel_parts" not in r
        assert "multipart_parcel" not in _codes(s)

    @pytest.mark.asyncio
    async def test_duplicated_rows_do_not_cancel_each_other(self, plugin):
        # The same polygon twice: concatenated rings would cancel under
        # the even-odd rule and the lot would vanish.
        rows = [
            _row([_square(0, 0, 20)], Lot_Size=4306),
            _row([_square(0, 0, 20)], Lot_Size=4306),
        ]
        _, s, _ = await _run(plugin, rows, [_building(5, 5, 10)])
        assert s["result"]["coverage_pct"] == 0.25
        assert s["result"]["mapped_area_sqft"] == round(400 * SQFT)


class TestWhenTheLotSizeDescribesOnlyOnePiece:
    """Lot_Size matches one piece and another rides along on the same
    number (165 Ocean Park Dr: two ~4,400 sqft pieces, Lot_Size 4,403)."""

    @pytest.mark.asyncio
    async def test_coverage_uses_the_land_measured_and_no_headroom_is_given(self, plugin):
        one_piece = round(400 * SQFT)
        rows = [
            _row([_square(0, 0, 20)], Lot_Size=one_piece),
            _row([_square(100, 0, 20)], Lot_Size=one_piece),
        ]
        text, s, _ = await _run(plugin, rows, [_building(5, 5, 10), _building(105, 5, 10)])
        r = s["result"]
        # 200 m2 over 800 m2 mapped, not over the 400 m2 Lot_Size (50%).
        assert r["coverage_pct"] == 0.25
        assert r["coverage_basis"] == "mapped_area"
        assert r["mapped_area_sqft"] == round(800 * SQFT)
        assert r["max_footprint_sqft"] is None
        assert r["adu_footprint_headroom_sqft"] is None
        assert "headroom_if_note3_50pct" not in r
        assert "multipart_lot_basis_uncertain" in _codes(s)
        assert "lot_area_disagreement" not in _codes(s)
        assert "of the mapped land (all pieces)" in text
        assert "Footprint headroom" not in text

    @pytest.mark.asyncio
    async def test_a_single_polygon_disagreement_keeps_the_old_caveat(self, plugin):
        rows = [_row([_square(0, 0, 20)], Lot_Size=9000)]
        _, s, _ = await _run(plugin, rows, [_building(5, 5, 10)])
        assert "lot_area_disagreement" in _codes(s)
        assert "multipart_lot_basis_uncertain" not in _codes(s)


class TestRowCap:
    @pytest.mark.asyncio
    async def test_hitting_the_row_cap_is_said(self, plugin):
        plugin.FOOTPRINT_PARCEL_ROW_LIMIT = 2
        total_sqft = round(2 * 400 * SQFT)
        rows = [
            _row([_square(0, 0, 20)], Lot_Size=total_sqft),
            _row([_square(100, 0, 20)], Lot_Size=total_sqft),
        ]
        _, s, _ = await _run(plugin, rows, [])
        assert "parcel_parts_cap_reached" in _codes(s)
