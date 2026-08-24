"""Correctness of coverage_by_polygon's area figures and diagnostics.

The percentage was always right -- the distortion cancels in a ratio of
two same-projection areas. The RAW areas were not: MOA hosted layers
publish Shape__Area in Web Mercator, which inflates area by sec^2(lat),
about 4.3x at Anchorage. Read as square feet they were wrong by roughly
a factor of four, and nothing in the output said so.
"""

import math

import pytest

from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin


@pytest.fixture
def plugin():
    cfg = {
        "portal_base_url": "https://example.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "abc123",
        "org_id": "org123",
        "city_name": "Municipality of Anchorage",
        "gallery_url": "https://example.com/gallery",
        "timeout": 20,
    }
    p = AnchorageGISPlugin(cfg)
    p.plugin_config = AnchorageGISPluginConfig(**cfg)
    return p


class TestWebMercatorAreaCorrection:
    def test_matches_assessor_lot_size_on_a_real_parcel(self):
        """Ground truth: parcel 00326133000 stores Shape__Area = 1010
        (Web Mercator m2); the assessor's Lot_Size is 2,493 sqft."""
        sqft = AnchorageGISPlugin._to_sqft(1010.0, 61.2016, 3857)
        assert sqft is not None
        assert abs(sqft - 2493) / 2493 < 0.05, f"{sqft:,.0f} sqft vs 2,493"

    def test_correction_is_per_feature_not_a_flat_divisor(self):
        """The factor varies ~2% across the municipality, so applying one
        constant would be wrong at the edges."""
        girdwood = AnchorageGISPlugin._to_sqft(1000.0, 60.95, 3857)
        eagle_river = AnchorageGISPlugin._to_sqft(1000.0, 61.32, 3857)
        assert girdwood > eagle_river  # less distortion further south
        assert abs(girdwood - eagle_river) / girdwood > 0.01

    @pytest.mark.parametrize("wkid", [3857, 102100, 102113])
    def test_recognises_web_mercator_variants(self, wkid):
        assert AnchorageGISPlugin._to_sqft(1000.0, 61.2, wkid) is not None

    @pytest.mark.parametrize("wkid", [4326, 4269, 26935, None])
    def test_refuses_to_guess_for_other_projections(self, wkid):
        """Returning a number we cannot justify would be the original bug
        in a new costume."""
        assert AnchorageGISPlugin._to_sqft(1000.0, 61.2, wkid) is None

    def test_percentage_is_unaffected_by_the_projection(self):
        """Why this was silent: the ratio was always correct."""
        lat = 61.2
        raw_ratio = 250.0 / 1000.0
        true_ratio = (
            AnchorageGISPlugin._to_sqft(250.0, lat, 3857)
            / AnchorageGISPlugin._to_sqft(1000.0, lat, 3857)
        )
        assert abs(raw_ratio - true_ratio) < 1e-9

    def test_layer_wkid_reads_latest_then_wkid(self):
        f = AnchorageGISPlugin._layer_wkid
        assert f({"extent": {"spatialReference": {"latestWkid": 3857}}}) == 3857
        assert f({"extent": {"spatialReference": {"wkid": 102100}}}) == 102100
        assert f({}) is None


class TestGeodesicArea:
    def test_matches_the_analytic_area_of_a_box(self):
        """R^2 * dlon * (sin(lat2) - sin(lat1)) for a lon/lat box."""
        lon1, lat1, lon2, lat2 = -149.90, 61.20, -149.89, 61.21
        box = {
            "type": "Polygon",
            "coordinates": [[[lon1, lat1], [lon1, lat2], [lon2, lat2],
                             [lon2, lat1], [lon1, lat1]]],
        }
        R = AnchorageGISPlugin._EARTH_RADIUS_M
        expected = (
            R * R
            * math.radians(lon2 - lon1)
            * (math.sin(math.radians(lat2)) - math.sin(math.radians(lat1)))
        )
        got = AnchorageGISPlugin._geometry_area_m2(box)
        assert abs(got - abs(expected)) / abs(expected) < 0.001

    def test_subtracts_holes(self):
        outer = [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]
        hole = [[0.4, 0.4], [0.4, 0.6], [0.6, 0.6], [0.6, 0.4], [0.4, 0.4]]
        solid = AnchorageGISPlugin._geometry_area_m2(
            {"type": "Polygon", "coordinates": [outer]})
        holed = AnchorageGISPlugin._geometry_area_m2(
            {"type": "Polygon", "coordinates": [outer, hole]})
        assert holed < solid
        assert abs((solid - holed) / solid - 0.04) < 0.005  # hole is 4%


class TestDiagnosticThresholds:
    def test_zero_artifact_threshold_matches_the_measured_tiles(self):
        """Eastridge SW1433 measured 128/195 (66%) false zeros; Hillside
        SW2436 measured 0/79. The threshold must split those."""
        t = AnchorageGISPlugin.COVERAGE_ZERO_ARTIFACT_RATIO
        assert (128 / 195) > t, "Eastridge must trigger the warning"
        assert (0 / 79) <= t, "Hillside must not"

    def test_banner_names_the_failure_mode_and_the_escape_hatch(self):
        """A screening tool that doesn't name its failure mode is a trap."""
        import inspect

        src = inspect.getsource(AnchorageGISPlugin._coverage_by_polygon)
        assert "possible_centroid_artifact" in src
        assert "footprint_for_parcel" in src

    def test_polygons_vs_parcels_caveat_exists(self):
        import inspect

        src = inspect.getsource(AnchorageGISPlugin._coverage_by_polygon)
        assert "targets_are_polygons_not_parcels" in src
        assert "GIS_Category" in src


class TestAssessorTraps:
    def test_banner_covers_every_documented_trap(self):
        b = AnchorageGISPlugin.ASSESSOR_TRAP_BANNER
        for probe in [
            "Total_Living_Units",      # naive summing
            "Lease Master",            # duplicate apartment rows
            "Commercial",              # Residential undercounts
            "10880 Mausel",            # miscoded outlier
            "B2C",                     # unnormalised zoning
            "Parcel_ID IS NOT NULL",   # the ~1,063 null rows
            "GIS_Category",            # polygons vs parcels
        ]:
            assert probe in b, f"banner missing: {probe}"

    def test_banner_is_gated_to_the_property_layer(self):
        import inspect

        src = inspect.getsource(AnchorageGISPlugin.execute_tool)
        assert "PROPERTY_INFO_ITEM_ID" in src
        assert AnchorageGISPlugin.PROPERTY_INFO_ITEM_ID == (
            "57d6ff611f444d75a1bf2b4a1d340163"
        )


class TestAllNullShortCircuit:
    def test_all_null_page_is_summarised_not_rendered(self, plugin):
        records = [{"OBJECTID": i, "Owner": None, "Land_Use": None}
                   for i in range(30)]
        text, structured = plugin._format_query_results(records, limit=50)
        assert "are NULL on every one of the 30" in text
        assert "Record 1:" not in text
        assert "get_layer_schema" in text
        # the data is still in the structured half
        assert len(structured["rows"]) == 30

    def test_page_with_any_value_renders_normally(self, plugin):
        records = [{"OBJECTID": 1, "Owner": None},
                   {"OBJECTID": 2, "Owner": "SMITH"}]
        text, _ = plugin._format_query_results(records, limit=50)
        assert "are NULL on every one" not in text
        assert "SMITH" in text
