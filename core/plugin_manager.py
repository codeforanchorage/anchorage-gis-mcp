"""Plugin Manager for OpenContext.

Handles plugin discovery, loading, validation, and tool routing.
Enforces the "one fork = one MCP server" rule at runtime.
"""

import importlib
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.interfaces import MCPPlugin, ToolResult, UnknownToolError
from core.validators import ConfigurationError, get_enabled_plugin_config

logger = logging.getLogger(__name__)

# Directories, relative to the plugin root, that hold plugin packages.
PLUGIN_DIRS = ("plugins", "custom_plugins")

# Set to the directory containing plugins/ (and custom_plugins/) to say
# explicitly where a deployment's plugins live.
PLUGIN_ROOT_ENV = "OPENCONTEXT_PLUGIN_ROOT"


def plugin_root() -> Path:
    """Directory that contains ``plugins/`` and ``custom_plugins/``.

    Resolved in this order:

    1. ``$OPENCONTEXT_PLUGIN_ROOT``, when set.
    2. The directory this ``core`` package sits in, when plugins are
       there too -- the layout of a repo that vendors ``core/`` next to
       ``plugins/``, and of the Lambda zip.
    3. ``$LAMBDA_TASK_ROOT``, else the current directory -- the case
       where ``core`` is an installed dependency living in site-packages
       and the deployment's own plugins are in its project directory.

    Plugins are imported as ``plugins.<name>.plugin``, so for 1 and 3 the
    root is put on ``sys.path`` if it is not importable already.
    """
    override = os.environ.get(PLUGIN_ROOT_ENV)
    if override:
        root = Path(override)
    else:
        package_parent = Path(__file__).parent.parent
        if any((package_parent / name).is_dir() for name in PLUGIN_DIRS):
            return package_parent
        root = Path(os.environ.get("LAMBDA_TASK_ROOT") or os.getcwd())

    root_str = str(root.resolve())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


class PluginManager:
    """Manages plugin discovery, loading, and tool routing.

    The Plugin Manager:
    - Auto-discovers plugins from plugins/ and custom_plugins/
    - Loads only the enabled plugin from config.yaml
    - Validates that exactly ONE plugin is enabled (crashes if multiple)
    - Registers tools with plugin name prefix
    - Routes tool calls to the correct plugin
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialize Plugin Manager with configuration.

        Args:
            config: Parsed configuration dictionary

        Raises:
            ConfigurationError: If multiple plugins are enabled
        """
        self.config = config
        self.plugins: Dict[str, MCPPlugin] = {}
        self.tools: Dict[
            str, Tuple[str, str]
        ] = {}  # tool_name -> (plugin_name, tool_name)
        self._initialized = False

    def discover_plugins(self) -> List[Tuple[str, Path]]:
        """Discover available plugins in plugins/ and custom_plugins/ directories.

        Returns:
            List of tuples (plugin_name, plugin_directory_path)
        """
        discovered = []
        base_dir = plugin_root()

        # Built-in plugins first, then custom plugins.
        for dir_name in PLUGIN_DIRS:
            plugins_dir = base_dir / dir_name
            if not plugins_dir.exists():
                continue
            for plugin_dir in sorted(plugins_dir.iterdir()):
                if plugin_dir.is_dir() and not plugin_dir.name.startswith("_"):
                    plugin_file = plugin_dir / "plugin.py"
                    if plugin_file.exists():
                        discovered.append((plugin_dir.name, plugin_dir))

        logger.debug(
            f"Discovered {len(discovered)} plugins: {[p[0] for p in discovered]}"
        )
        return discovered

    def _load_plugin_class(self, plugin_name: str, plugin_path: Path) -> type:
        """Load plugin class from a plugin module.

        Args:
            plugin_name: Name of the plugin
            plugin_path: Path to plugin directory

        Returns:
            Plugin class that inherits from MCPPlugin

        Raises:
            ImportError: If plugin cannot be imported
            ValueError: If plugin class not found or invalid
        """
        # The package is the directory the plugin sits in. Matched on that
        # directory's NAME, not by substring: "plugins" is a substring of
        # "custom_plugins" (so custom plugins used to be imported from the
        # wrong package) and of any unrelated parent directory.
        package = Path(plugin_path).parent.name
        if package not in PLUGIN_DIRS:
            raise ValueError(f"Invalid plugin path: {plugin_path}")
        module_path = f"{package}.{plugin_name}.plugin"

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"Failed to import plugin {plugin_name}: {e}")

        # Find class that inherits from MCPPlugin
        plugin_class = None
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj != MCPPlugin
                and issubclass(obj, MCPPlugin)
                and obj.__module__ == module.__name__
            ):
                plugin_class = obj
                break

        if plugin_class is None:
            raise ValueError(
                f"Plugin {plugin_name} does not define a class inheriting from MCPPlugin"
            )

        return plugin_class

    async def load_plugins(self) -> None:
        """Load plugins based on configuration.

        CRITICAL: This method validates that exactly ONE plugin is enabled.
        If multiple plugins are enabled, it raises a fatal error that will
        crash the Lambda function.

        Raises:
            ConfigurationError: If zero or multiple plugins are enabled
            ImportError: If plugin cannot be imported
            RuntimeError: If plugin initialization fails
        """
        # Get enabled plugin config (validates count == 1)
        try:
            plugin_name, plugin_config = get_enabled_plugin_config(self.config)
        except ConfigurationError as e:
            # Log error and re-raise to crash Lambda
            logger.error(f"Plugin configuration error: {e}")
            raise

        # Discover all plugins
        discovered = self.discover_plugins()
        discovered_names = [p[0] for p in discovered]

        if plugin_name not in discovered_names:
            raise RuntimeError(
                f"Plugin '{plugin_name}' is enabled in config.yaml but not found.\n"
                f"Available plugins: {', '.join(discovered_names)}\n"
                f"Check that plugins/{plugin_name}/plugin.py exists, or\n"
                f"custom_plugins/{plugin_name}/plugin.py exists."
            )

        # Find plugin path
        plugin_path = next(p[1] for p in discovered if p[0] == plugin_name)

        # Load plugin class
        try:
            plugin_class = self._load_plugin_class(plugin_name, plugin_path)
        except (ImportError, ValueError) as e:
            logger.error(f"Failed to load plugin {plugin_name}: {e}")
            raise RuntimeError(f"Failed to load plugin {plugin_name}: {e}") from e

        # Instantiate plugin
        try:
            plugin_instance = plugin_class(plugin_config)
        except Exception as e:
            logger.error(f"Failed to instantiate plugin {plugin_name}: {e}")
            raise RuntimeError(
                f"Failed to instantiate plugin {plugin_name}: {e}"
            ) from e

        # Initialize plugin
        try:
            initialized = await plugin_instance.initialize()
            if not initialized:
                raise RuntimeError(
                    f"Plugin {plugin_name} initialization returned False"
                )
        except Exception as e:
            logger.error(f"Failed to initialize plugin {plugin_name}: {e}")
            raise RuntimeError(f"Failed to initialize plugin {plugin_name}: {e}") from e

        # Store plugin
        self.plugins[plugin_name] = plugin_instance

        # Register tools
        self._register_tools(plugin_name, plugin_instance)

        logger.info(
            f"Successfully loaded plugin: {plugin_name} "
            f"(type: {plugin_instance.plugin_type}, "
            f"version: {plugin_instance.plugin_version})"
        )

        self._initialized = True

    def _register_tools(self, plugin_name: str, plugin: MCPPlugin) -> None:
        """Register tools from a plugin.

        Args:
            plugin_name: Name of the plugin
            plugin: Plugin instance
        """
        tools = plugin.get_tools()

        for tool_def in tools:
            # Create prefixed tool name: plugin_name__tool_name
            # Using double underscore to avoid conflicts with tool names that contain underscores
            prefixed_name = f"{plugin_name}__{tool_def.name}"

            if prefixed_name in self.tools:
                logger.warning(f"Tool {prefixed_name} already registered, overwriting")

            self.tools[prefixed_name] = (plugin_name, tool_def.name)
            logger.debug(f"Registered tool: {prefixed_name}")

        logger.info(f"Registered {len(tools)} tools from plugin {plugin_name}")

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        """Execute a tool by name.

        Args:
            tool_name: Full tool name (e.g., "ckan__search_datasets")
            arguments: Tool input arguments

        Returns:
            ToolResult with content and success flag

        Raises:
            ValueError: If tool not found
            RuntimeError: If plugin execution fails
        """
        if not self._initialized:
            raise RuntimeError(
                "Plugin Manager not initialized. Call load_plugins() first."
            )

        if tool_name not in self.tools:
            available = ", ".join(sorted(self.tools.keys()))
            raise UnknownToolError(tool_name, available)

        plugin_name, actual_tool_name = self.tools[tool_name]
        plugin = self.plugins.get(plugin_name)

        if plugin is None:
            raise RuntimeError(f"Plugin {plugin_name} not loaded")

        try:
            result = await plugin.execute_tool(actual_tool_name, arguments)
            return result
        except Exception as e:
            logger.error(
                f"Error executing tool {tool_name} in plugin {plugin_name}: {e}",
                exc_info=True,
            )
            error_msg = str(e) if str(e) else "Tool execution failed"
            return ToolResult(
                content=[],
                success=False,
                error_message=error_msg,
            )

    def get_instructions(self) -> Optional[str]:
        """Server-level instructions supplied by the loaded plugin(s).

        Returns:
            The plugins' instruction texts joined by blank lines, or None
            when no plugin supplies any.
        """
        parts = []
        for plugin in self.plugins.values():
            text = plugin.get_instructions()
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts) or None

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Get all registered tools with their definitions.

        Returns:
            List of tool definitions with prefixed names
        """
        tools = []

        for plugin_name, plugin in self.plugins.items():
            plugin_tools = plugin.get_tools()
            for tool_def in plugin_tools:
                # Use double underscore separator to match _register_tools
                prefixed_name = f"{plugin_name}__{tool_def.name}"
                tool_dict: Dict[str, Any] = {
                    "name": prefixed_name,
                    "description": tool_def.description,
                    "inputSchema": tool_def.input_schema,
                }
                # `title` is a top-level Tool field, not an annotation. It is
                # the display name clients prefer over the prefixed `name`,
                # which is an identifier and reads poorly in a picker.
                if tool_def.title:
                    tool_dict["title"] = tool_def.title
                if tool_def.output_schema:
                    tool_dict["outputSchema"] = tool_def.output_schema
                if tool_def.annotations:
                    tool_dict["annotations"] = tool_def.annotations
                tools.append(tool_dict)

        return tools

    async def health_check(self) -> Dict[str, bool]:
        """Check health of all loaded plugins.

        Returns:
            Dictionary mapping plugin names to health status
        """
        health = {}

        for plugin_name, plugin in self.plugins.items():
            try:
                health[plugin_name] = await plugin.health_check()
            except Exception as e:
                logger.error(f"Health check failed for {plugin_name}: {e}")
                health[plugin_name] = False

        return health

    async def shutdown(self) -> None:
        """Shutdown all plugins and clean up resources."""
        for plugin_name, plugin in self.plugins.items():
            try:
                await plugin.shutdown()
                logger.info(f"Shutdown plugin: {plugin_name}")
            except Exception as e:
                logger.error(f"Error shutting down plugin {plugin_name}: {e}")

        self.plugins.clear()
        self.tools.clear()
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        """Check if Plugin Manager has been initialized."""
        return self._initialized
