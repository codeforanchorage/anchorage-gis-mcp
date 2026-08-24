"""Caller mistakes must not be logged as server faults.

A traceback is a claim that the server broke. Spending one on "you
forgot item_id" is what made the original -32603 noise unreadable: real
faults were buried under argument-validation errors that looked
identical in CloudWatch.

ToolInputError is an explicit marker rather than an inference from
ValueError, and these tests pin the two cases where the inference would
have been wrong.
"""

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.interfaces import ToolInputError
from core.mcp_server import MCPServer
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)


@pytest.fixture
def plugin():
    cfg = {
        "portal_base_url": "https://example.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "abc123",
        "org_id": "org123",
        "city_name": "Municipality of Anchorage",
        "gallery_url": "https://example.com/gallery",
        "timeout": 30,
    }
    p = AnchorageGISPlugin(cfg)
    p.plugin_config = AnchorageGISPluginConfig(**cfg)
    return p


class TestValidatorsRaiseToolInputError:
    @pytest.mark.parametrize("bad", [
        "1=1; DROP TABLE x",
        "1=1 UNION SELECT * FROM y",
        "1=1 OR 1=1--",
        "Name = 'unbalanced",
    ])
    def test_where_rejections_are_caller_errors(self, bad):
        with pytest.raises(ToolInputError):
            WhereValidator.validate(bad)

    def test_out_fields_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            OutFieldsValidator.validate("a; DROP TABLE x")

    def test_order_by_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            OrderByValidator.validate("name; DROP TABLE x")

    def test_still_catchable_as_valueerror(self):
        """Subclassing ValueError keeps every existing handler working."""
        with pytest.raises(ValueError):
            WhereValidator.validate("1=1; DROP TABLE x")


class TestNotInferredFromValueError:
    """The two cases where treating ValueError as a caller error is wrong."""

    def test_json_decode_error_is_not_a_tool_input_error(self):
        """json.JSONDecodeError subclasses ValueError. If ValueError were
        the marker, a malformed upstream payload would be misfiled as a
        caller mistake and silently lose its stack trace."""
        try:
            json.loads("{not json")
        except json.JSONDecodeError as e:
            assert isinstance(e, ValueError)
            assert not isinstance(e, ToolInputError)
        else:
            pytest.fail("expected JSONDecodeError")

    def test_upstream_non_json_stays_a_plain_valueerror(self):
        """The ArcGIS wrapper raises plain ValueError for a non-JSON
        response -- a genuine upstream fault whose traceback we want."""
        import inspect

        src = inspect.getsource(AnchorageGISPlugin)
        # Every remaining `raise ValueError` in the plugin must be an
        # upstream fault, not caller input.
        for line in src.splitlines():
            if "raise ValueError(" in line:
                assert "ToolInputError" not in line
        assert src.count("raise ValueError(") == 4, (
            "expected exactly the 4 non-JSON upstream raises; a new "
            "plain ValueError was added -- classify it deliberately"
        )


class TestOuterHandlerLogging:
    @pytest.mark.asyncio
    async def test_caller_error_logs_warning_without_traceback(
        self, plugin, caplog
    ):
        with patch.object(
            plugin,
            "_find_gis_content",
            AsyncMock(side_effect=ToolInputError("topic is required")),
        ):
            with caplog.at_level(logging.WARNING):
                result = await plugin.execute_tool("find_gis_content", {})

        assert result.success is False
        assert "topic is required" in result.error_message
        recs = [r for r in caplog.records if "find_gis_content" in r.getMessage()]
        assert recs, "expected a log record"
        assert all(r.levelno == logging.WARNING for r in recs)
        assert all(r.exc_info is None for r in recs), "no traceback for caller errors"

    @pytest.mark.asyncio
    async def test_server_fault_still_logs_error_with_traceback(
        self, plugin, caplog
    ):
        """The quiet path must not swallow genuine failures."""
        with patch.object(
            plugin,
            "_find_gis_content",
            AsyncMock(side_effect=RuntimeError("upstream exploded")),
        ):
            with caplog.at_level(logging.WARNING):
                result = await plugin.execute_tool("find_gis_content", {})

        assert result.success is False
        errs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errs, "a real fault must still log at ERROR"
        assert any(r.exc_info is not None for r in errs), "with a traceback"

    @pytest.mark.asyncio
    async def test_malformed_json_body_logs_warning_not_error(self, caplog):
        """-32700 already tells the caller; our parse traceback adds nothing."""
        from unittest.mock import MagicMock

        from core.plugin_manager import PluginManager

        server = MCPServer(MagicMock(spec=PluginManager))
        with caplog.at_level(logging.WARNING):
            resp = await server.handle_http_request("{not json")

        assert json.loads(resp["body"])["error"]["code"] == -32700
        recs = [r for r in caplog.records if "Invalid JSON" in r.getMessage()]
        assert recs
        assert all(r.levelno == logging.WARNING for r in recs)
        assert all(r.exc_info is None for r in recs)


class TestNumericCoercion:
    @pytest.mark.asyncio
    async def test_non_numeric_limit_gives_a_readable_error(self, plugin):
        """A bare int() ValueError reads as a server fault and tells the
        caller nothing useful."""
        result = await plugin.execute_tool(
            "browse_gallery", {"limit": "not-a-number"}
        )
        assert result.success is False
        assert "limit" in result.error_message
        assert "not-a-number" in result.error_message
