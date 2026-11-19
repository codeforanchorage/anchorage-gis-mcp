"""Correctness of coverage_by_polygon's area figures and diagnostics.

The percentage was always right -- the distortion cancels in a ratio of
two same-projection areas. The RAW areas were not: MOA hosted layers
publish Shape__Area in Web Mercator, which inflates area by sec^2(lat),
about 4.3x at Anchorage. Read as square feet they were wrong by roughly
a factor of four, and nothing in the output said so.
"""


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


class TestDiagnosticThresholds:

    def test_banner_names_the_failure_mode_and_the_escape_hatch(self):
        """A screening tool that doesn't name its failure mode is a trap."""
        import inspect

        src = inspect.getsource(AnchorageGISPlugin._coverage_by_polygon)
        assert "possible_centroid_artifact" in src
        # The escape hatch is region wording: the shared base renders
        # TEXT["coverage_verify_hint"], and Anchorage's names the tool.
        assert "coverage_verify_hint" in src
        assert "footprint_for_parcel" in (
            AnchorageGISPlugin.TEXT["coverage_verify_hint"]
        )

    def test_polygons_vs_parcels_caveat_exists(self):
        import inspect

        src = inspect.getsource(AnchorageGISPlugin._coverage_by_polygon)
        assert "targets_are_polygons_not_parcels" in src
        assert "stacked_records_note" in src
        assert "GIS_Category" in (
            AnchorageGISPlugin.TEXT["stacked_records_note"]
        )


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
        assert AnchorageGISPlugin.PROPERTY_INFO_ITEM_ID == (
            "57d6ff611f444d75a1bf2b4a1d340163"
        )
        # Anchorage answers for the property layer only.
        p = AnchorageGISPlugin({})
        assert (
            p._query_result_banner(AnchorageGISPlugin.PROPERTY_INFO_ITEM_ID)
            == AnchorageGISPlugin.ASSESSOR_TRAP_BANNER
        )
        assert p._query_result_banner("0" * 32) is None



    def test_banner_reaches_the_structured_channel_too(self):
        """Clients that render structuredContent alone must see the
        traps: the banner used to be prepended to the markdown only."""
        p = AnchorageGISPlugin({})
        structured = {"caveats": [{"code": "x", "message": "y"}]}
        text = p._apply_result_banner(
            AnchorageGISPlugin.PROPERTY_INFO_ITEM_ID, "rows", structured
        )
        assert text.startswith(AnchorageGISPlugin.ASSESSOR_TRAP_BANNER)
        assert structured["caveats"][0] == {
            "code": "layer_notes",
            "message": AnchorageGISPlugin.ASSESSOR_TRAP_BANNER,
        }
        # Any other layer is left alone.
        other = {"caveats": []}
        assert p._apply_result_banner("0" * 32, "rows", other) == "rows"
        assert other["caveats"] == []
