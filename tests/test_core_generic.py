"""Tests for the pieces that let one core serve every deployment.

Nothing here refers to a particular plugin or deployment: HTTP settings
come from config, plugins are found wherever the deployment keeps them,
and a plugin may supply the server-level instructions.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core import plugin_manager as plugin_manager_module
from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from core.mcp_server import MCPServer
from core.plugin_manager import PLUGIN_ROOT_ENV, PluginManager, plugin_root
from core.validators import (
    DEFAULT_ALLOWED_ORIGINS,
    ConfigurationError,
    get_http_settings,
    load_and_validate_config,
)
from server import http_handler
from server.http_handler import UniversalHTTPHandler

BASE_CONFIG = {"plugins": {"stub": {"enabled": True}}}


class StubPlugin(MCPPlugin):
    """Minimal plugin; `instructions` is what get_instructions returns."""

    plugin_name = "stub"
    plugin_type = PluginType.CUSTOM_API

    def __init__(self, config, instructions=None):
        super().__init__(config)
        self._instructions_text = instructions

    async def initialize(self) -> bool:
        self._initialized = True
        return True

    async def shutdown(self) -> None:
        self._initialized = False

    def get_instructions(self):
        return self._instructions_text

    def get_tools(self):
        return [
            ToolDefinition(
                name="echo",
                description="Echo",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def execute_tool(self, tool_name, arguments):
        return ToolResult(content=[], success=True)

    async def health_check(self) -> bool:
        return True


def _manager(config, instructions=None) -> PluginManager:
    manager = PluginManager(config)
    manager.plugins["stub"] = StubPlugin({}, instructions)
    manager._initialized = True
    return manager


class TestHttpSettings:
    def test_defaults(self):
        settings = get_http_settings(BASE_CONFIG)
        assert settings["allowed_origins"] == frozenset(DEFAULT_ALLOWED_ORIGINS)
        assert settings["default_cors_origin"] == "https://claude.ai"
        assert settings["mcp_paths"] == ("/mcp",)

    def test_overrides_replace_the_defaults(self):
        settings = get_http_settings(
            {
                **BASE_CONFIG,
                "server_allowed_origins": [
                    "https://one.example",
                    "https://two.example",
                ],
                "server_mcp_paths": ["/mcp", "/mcp-keyed", "/mcp"],
            }
        )
        assert settings["allowed_origins"] == frozenset(
            {"https://one.example", "https://two.example"}
        )
        assert settings["default_cors_origin"] == "https://one.example"
        # Configured order kept, duplicates dropped.
        assert settings["mcp_paths"] == ("/mcp", "/mcp-keyed")

    @pytest.mark.parametrize(
        "bad",
        [
            {"server_mcp_paths": "/mcp"},
            {"server_mcp_paths": []},
            {"server_mcp_paths": ["mcp"]},
            {"server_mcp_paths": ["/mcp", 7]},
            {"server_allowed_origins": "https://claude.ai"},
            {"server_allowed_origins": []},
            {"server_allowed_origins": ["claude.ai"]},
            {"server_allowed_origins": ["https://claude.ai/"]},
        ],
    )
    def test_malformed_settings_are_rejected(self, bad):
        with pytest.raises(ConfigurationError):
            get_http_settings({**BASE_CONFIG, **bad})

    def test_malformed_settings_fail_config_load(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "server_mcp_paths: mcp\nplugins:\n  stub:\n    enabled: true\n"
        )
        with pytest.raises(ConfigurationError):
            load_and_validate_config(str(path))


class TestHandlerUsesConfiguredSettings:
    @staticmethod
    async def _post(path, headers=None):
        handler = UniversalHTTPHandler()
        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_server,
        ):
            mock_server.handle_http_request = AsyncMock(
                return_value={"statusCode": 200, "headers": {}, "body": "{}"}
            )
            return await handler.handle_request(
                method="POST",
                path=path,
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers=headers or {},
            )

    @pytest.mark.asyncio
    async def test_only_mcp_is_served_by_default(self):
        http_handler._config = dict(BASE_CONFIG)
        assert (await self._post("/mcp"))[0] == 200
        status, _, body = await self._post("/mcp-gcc")
        assert status == 404
        assert json.loads(body)["error"]["data"].endswith("Expected '/mcp'")

    @pytest.mark.asyncio
    async def test_configured_extra_path_is_served(self):
        http_handler._config = {
            **BASE_CONFIG,
            "server_mcp_paths": ["/mcp", "/mcp-keyed"],
        }
        assert (await self._post("/mcp-keyed"))[0] == 200
        status, _, body = await self._post("/other")
        assert status == 404
        assert "Expected '/mcp' or '/mcp-keyed'" in body

    @pytest.mark.asyncio
    async def test_configured_origins_are_enforced(self):
        http_handler._config = {
            **BASE_CONFIG,
            "server_allowed_origins": ["https://one.example"],
        }
        status, headers, _ = await self._post(
            "/mcp", {"origin": "https://one.example"}
        )
        assert status == 200
        assert headers["Access-Control-Allow-Origin"] == "https://one.example"
        # A default origin is no longer allowed once the list is replaced.
        status, headers, _ = await self._post(
            "/mcp", {"origin": "https://claude.ai"}
        )
        assert status == 403
        assert headers["Access-Control-Allow-Origin"] == "https://one.example"

    @pytest.mark.asyncio
    async def test_unloadable_config_falls_back_to_defaults(self):
        """The origin/path checks run before initialization and must not
        crash when the config cannot be loaded."""
        with patch(
            "server.http_handler._load_config",
            side_effect=FileNotFoundError("no config"),
        ):
            status, _, _ = await self._post(
                "/mcp", {"origin": "https://evil.example"}
            )
            assert status == 403
            assert (await self._post("/nope"))[0] == 404
            assert (await self._post("/mcp"))[0] == 200


class TestPluginRoot:
    @pytest.fixture
    def project(self, tmp_path):
        """A deployment directory holding one plugin in each package."""
        for package, name in (("plugins", "alpha"), ("custom_plugins", "beta")):
            plugin_dir = tmp_path / package / name
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.py").write_text("# plugin\n")
        saved_path = list(sys.path)
        yield tmp_path
        sys.path[:] = saved_path

    def test_vendored_layout_uses_the_core_packages_parent(self, monkeypatch):
        monkeypatch.delenv(PLUGIN_ROOT_ENV, raising=False)
        expected = Path(plugin_manager_module.__file__).parent.parent
        assert plugin_root() == expected

    def test_env_override_wins(self, project, monkeypatch):
        monkeypatch.setenv(PLUGIN_ROOT_ENV, str(project))
        assert plugin_root() == project
        assert str(project.resolve()) in sys.path
        names = [name for name, _ in PluginManager(BASE_CONFIG).discover_plugins()]
        assert names == ["alpha", "beta"]

    @staticmethod
    def _as_installed_dependency(monkeypatch, tmp_path):
        """Make core look installed in a site-packages with no plugins."""
        site_packages = tmp_path / "site-packages" / "core"
        site_packages.mkdir(parents=True)
        monkeypatch.delenv(PLUGIN_ROOT_ENV, raising=False)
        monkeypatch.setattr(
            plugin_manager_module,
            "__file__",
            str(site_packages / "plugin_manager.py"),
        )

    def test_installed_core_uses_lambda_task_root(
        self, project, monkeypatch, tmp_path
    ):
        self._as_installed_dependency(monkeypatch, tmp_path)
        monkeypatch.setenv("LAMBDA_TASK_ROOT", str(project))
        assert plugin_root() == project

    def test_installed_core_falls_back_to_cwd(
        self, project, monkeypatch, tmp_path
    ):
        self._as_installed_dependency(monkeypatch, tmp_path)
        monkeypatch.delenv("LAMBDA_TASK_ROOT", raising=False)
        monkeypatch.chdir(project)
        assert plugin_root() == project
        names = [name for name, _ in PluginManager(BASE_CONFIG).discover_plugins()]
        assert names == ["alpha", "beta"]

    def test_custom_plugin_is_imported_from_its_own_package(self):
        """ "plugins" is a substring of "custom_plugins"; the module path
        must come from the directory name, not a substring match."""
        manager = PluginManager(BASE_CONFIG)
        with patch(
            "core.plugin_manager.importlib.import_module",
            side_effect=ImportError("stop"),
        ) as mock_import:
            with pytest.raises(ImportError):
                manager._load_plugin_class(
                    "beta", Path("/srv/app/custom_plugins/beta")
                )
        mock_import.assert_called_once_with("custom_plugins.beta.plugin")

    def test_unrelated_directory_named_like_a_substring_is_rejected(self):
        manager = PluginManager(BASE_CONFIG)
        with pytest.raises(ValueError):
            manager._load_plugin_class("x", Path("/srv/my_plugins_old/x"))


class TestInstructions:
    @staticmethod
    async def _initialize(manager):
        response = await MCPServer(manager).handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        return response["result"]

    @pytest.mark.asyncio
    async def test_omitted_when_neither_config_nor_plugin_supplies_any(self):
        result = await self._initialize(_manager(dict(BASE_CONFIG)))
        assert "instructions" not in result

    @pytest.mark.asyncio
    async def test_plugin_supplies_instructions(self):
        result = await self._initialize(
            _manager(dict(BASE_CONFIG), instructions="  From the plugin.\n")
        )
        assert result["instructions"] == "From the plugin."

    @pytest.mark.asyncio
    async def test_config_overrides_the_plugin(self):
        config = {**BASE_CONFIG, "instructions": "From config."}
        result = await self._initialize(
            _manager(config, instructions="From the plugin.")
        )
        assert result["instructions"] == "From config."

    @pytest.mark.asyncio
    async def test_non_string_plugin_value_is_ignored(self):
        result = await self._initialize(
            _manager(dict(BASE_CONFIG), instructions=["not", "text"])
        )
        assert "instructions" not in result
