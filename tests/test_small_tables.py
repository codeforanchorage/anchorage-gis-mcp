"""query_data's small-table rule and its truncation guidance.

Prod evidence (Nov 2026): CommunityCouncil_Links_Hosted has 39 rows, and
claude.ai read it as 8 OBJECTID-range chunks. With no `limit` the tool
returned "25 of 39, TRUNCATED" and advised narrowing the WHERE clause --
an invitation to range-page. Now an omitted limit returns a table of up
to SMALL_TABLE_ROW_THRESHOLD rows whole and says it is complete, while
an explicit limit is still honoured (limit=1 is the counting idiom).
"""

from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin

THRESHOLD = AnchorageGISPlugin.SMALL_TABLE_ROW_THRESHOLD
QR_SCHEMA = AnchorageGISPlugin.QUERY_RESULT_OUTPUT_SCHEMA
ITEM = "934783d347ee4df5a0c12bd2d0339045"
LONG_TEXT = "https://communitycouncils.org/" + "x" * 470  # 500 chars


@pytest.fixture
def plugin():
    config = {
        "portal_base_url": "https://example.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "abc123",
        "org_id": "org123",
        "city_name": "Municipality of Anchorage",
        "gallery_url": "https://example.com/gallery",
        "timeout": 30,
    }
    p = AnchorageGISPlugin(config)
    p.plugin_config = AnchorageGISPluginConfig(**config)
    return p


def _rows(n):
    """A links-style table: long URL/text values in every row."""
    return [
        {
            "OBJECTID": i,
            "COUNCIL": f"Council {i}",
            "COUNCIL_URL": LONG_TEXT,
            "COUNCIL_MAP": f"http://cdn.example.com/maps/council_{i}.pdf",
        }
        for i in range(1, n + 1)
    ]


async def _run(plugin, total, args, *, count=None):
    """Call query_data against a fake layer holding `total` rows.

    The fake honours the requested row count the way ArcGIS's
    resultRecordCount does. Returns (result, limit the server was asked
    for). `count` overrides what the count query reports (None = the
    count failed).
    """
    table = _rows(total)
    fetch = AsyncMock(
        side_effect=lambda item_id, filters, limit, **kw: table[:limit]
    )
    reported = total if count is ... else count
    with (
        patch.object(
            plugin,
            "get_dataset",
            AsyncMock(
                return_value={
                    "url": "https://example.com/arcgis/rest/services/"
                    "L/FeatureServer",
                    "type": "Feature Service",
                }
            ),
        ),
        patch.object(
            plugin, "_get_layer_quick_meta", AsyncMock(return_value={})
        ),
        patch.object(plugin, "query_data", fetch),
        patch.object(
            plugin, "_get_record_count", AsyncMock(return_value=reported)
        ),
    ):
        result = await plugin.execute_tool(
            "query_data", {"item_id": ITEM, **args}
        )
    assert result.success, result.error_message
    Draft202012Validator(QR_SCHEMA).validate(result.structured_content)
    return result, fetch.call_args.args[2]


def _text(result):
    return result.content[0]["text"]


class TestSmallTableComesBackWhole:
    @pytest.mark.asyncio
    async def test_39_row_table_returns_every_row_in_one_call(self, plugin):
        """The prod case: no limit, 39 rows of long text."""
        result, asked = await _run(plugin, 39, {}, count=...)

        sc = result.structured_content
        assert sc["summary"] == {
            "returned": 39,
            "total_count": 39,
            "truncated": False,
            "complete": True,
        }
        assert [r["OBJECTID"] for r in sc["rows"]] == list(range(1, 40))
        text = _text(result)
        assert (
            "All 39 of 39 rows returned -- complete, no paging needed."
            in text
        )
        assert "default limit of 25 was lifted" in text
        assert "TRUNCATED" not in text
        # One upstream fetch, sized to the threshold, in parallel with
        # the count -- not a second round-trip.
        assert asked == THRESHOLD

    @pytest.mark.asyncio
    async def test_long_text_values_are_not_cut(self, plugin):
        """No row is dropped and no field is shortened to make room."""
        result, _ = await _run(plugin, 39, {}, count=...)
        rows = result.structured_content["rows"]
        assert all(r["COUNCIL_URL"] == LONG_TEXT for r in rows)
        assert _text(result).count(LONG_TEXT) == 39

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_is_complete(self, plugin):
        result, _ = await _run(plugin, THRESHOLD, {}, count=...)
        assert result.structured_content["summary"]["complete"] is True
        assert result.structured_content["summary"]["returned"] == THRESHOLD

    @pytest.mark.asyncio
    async def test_one_over_threshold_keeps_the_default(self, plugin):
        result, _ = await _run(plugin, THRESHOLD + 1, {}, count=...)
        summary = result.structured_content["summary"]
        assert summary["returned"] == 25
        assert summary["truncated"] is True
        assert summary["complete"] is False

    @pytest.mark.asyncio
    async def test_table_under_the_default_is_complete_without_note(
        self, plugin
    ):
        result, _ = await _run(plugin, 12, {}, count=...)
        text = _text(result)
        assert "All 12 of 12 rows returned" in text
        assert "was lifted" not in text  # nothing needed lifting


class TestExplicitLimitIsHonoured:
    @pytest.mark.asyncio
    async def test_limit_1_stays_a_count(self, plugin):
        """limit=1 is the documented counting idiom: one row, no more."""
        result, asked = await _run(plugin, 39, {"limit": 1}, count=...)
        assert asked == 1
        sc = result.structured_content
        assert sc["summary"]["returned"] == 1
        assert sc["summary"]["truncated"] is True
        # ...but the hint names the one call that gets everything.
        assert "limit=39" in _text(result)

    @pytest.mark.asyncio
    async def test_explicit_limit_covering_the_table_says_complete(
        self, plugin
    ):
        result, asked = await _run(plugin, 39, {"limit": 50}, count=...)
        assert asked == 50
        text = _text(result)
        assert "All 39 of 39 rows returned" in text
        assert "was lifted" not in text

    @pytest.mark.asyncio
    async def test_explicit_25_is_not_expanded(self, plugin):
        """Asking for the default value explicitly is still a choice."""
        result, asked = await _run(plugin, 39, {"limit": 25}, count=...)
        assert asked == 25
        assert result.structured_content["summary"]["returned"] == 25


class TestLargeTablesKeepTruncatingWithABetterHint:
    @pytest.mark.asyncio
    async def test_under_one_call_cap_names_the_exact_limit(self, plugin):
        result, _ = await _run(plugin, 250, {}, count=...)
        sc = result.structured_content
        assert sc["summary"]["returned"] == 25
        assert sc["summary"]["truncated"] is True
        text = _text(result)
        assert "repeat this call ONCE with limit=250" in text
        assert "Do not page by OBJECTID ranges" in text
        # The old hint that read as "filter your way to completeness".
        assert "narrow the WHERE clause to get a complete" not in text
        caveat = next(
            c for c in sc["caveats"] if c["code"] == "result_truncated"
        )
        assert "limit=250" in caveat["message"]

    @pytest.mark.asyncio
    async def test_over_one_call_cap_points_at_filters(self, plugin):
        """Past 1,000 rows no single call returns everything."""
        result, _ = await _run(plugin, 120, {}, count=3456)
        text = _text(result)
        assert "3,456 rows exceed the 1,000-row cap" in text
        assert "Narrow `where`" in text
        assert "limit=3456" not in text

    @pytest.mark.asyncio
    async def test_failed_count_falls_back_to_the_default(self, plugin):
        """Without a count the table size is unknown: behave as before."""
        result, _ = await _run(plugin, 39, {}, count=None)
        sc = result.structured_content
        assert sc["summary"]["returned"] == 25
        assert sc["summary"]["total_count"] is None
        assert sc["summary"]["complete"] is False


class TestToolContract:
    def test_limit_declares_no_schema_default(self, plugin):
        """A client that pre-fills schema defaults would send limit=25
        and silently switch the small-table rule off."""
        tool = next(t for t in plugin.get_tools() if t.name == "query_data")
        limit = tool.input_schema["properties"]["limit"]
        assert "default" not in limit
        assert str(THRESHOLD) in limit["description"]
        assert "OBJECTID" in tool.description

    def test_empty_result_is_complete(self):
        payload = AnchorageGISPlugin._empty_query_result(
            item_id=ITEM, where="1=0", out_fields=None, limit=25
        )
        assert payload["summary"]["complete"] is True
        Draft202012Validator(QR_SCHEMA).validate(payload)
