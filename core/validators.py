"""Configuration validation functions for OpenContext.

This module provides validation functions to ensure configuration is correct,
with special emphasis on enforcing the "one fork = one MCP server" rule.
"""

import logging
from typing import Any, Dict, List, Tuple

import yaml

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""

    pass


def validate_plugin_count(config: Dict[str, Any]) -> Tuple[List[str], int]:
    """Validate that exactly ONE plugin is enabled in the configuration.

    This is a critical validation that enforces the "one fork = one MCP server"
    architecture principle.

    Args:
        config: Parsed configuration dictionary

    Returns:
        Tuple of (enabled_plugin_names, count)

    Raises:
        ConfigurationError: If zero or multiple plugins are enabled
    """
    plugins_config = config.get("plugins", {})
    enabled_plugins = []

    for plugin_name, plugin_config in plugins_config.items():
        if isinstance(plugin_config, dict) and plugin_config.get("enabled", False):
            enabled_plugins.append(plugin_name)

    count = len(enabled_plugins)

    if count == 0:
        raise ConfigurationError(
            "ERROR: Configuration Error: No Plugins Enabled\n\n"
            "You must enable exactly ONE plugin in config.yaml.\n\n"
            "To enable a plugin, set 'enabled: true' for:\n"
            "  * ckan\n"
            "  * A custom plugin in custom_plugins/\n\n"
            "See docs/GETTING_STARTED.md for setup instructions."
        )

    if count > 1:
        plugin_list = "\n".join(f"  * {name}" for name in enabled_plugins)
        raise ConfigurationError(
            f"ERROR: Configuration Error: Multiple Plugins Enabled\n\n"
            f"You have {count} plugins enabled in config.yaml:\n{plugin_list}\n\n"
            f"OpenContext enforces: One Fork = One MCP Server\n\n"
            f"This keeps deployments:\n"
            f"  [+] Simple and focused\n"
            f"  [+] Independently scalable\n"
            f"  [+] Easy to maintain\n\n"
            f"To deploy multiple MCP servers:\n\n"
            f"  1. Fork this repository again\n"
            f"     Example: opencontext-opendata, opencontext-mbta\n\n"
            f"  2. Configure ONE plugin per fork\n"
            f"     Fork #1: Enable {enabled_plugins[0]} only\n"
            f"     Fork #2: Enable {enabled_plugins[1]} only\n\n"
            f"  3. Deploy each fork separately\n"
            f"     ./scripts/deploy.sh (in each fork)\n\n"
            f"See docs/ARCHITECTURE.md for details."
        )

    return enabled_plugins, count


def validate_config_structure(config: Dict[str, Any]) -> None:
    """Validate basic configuration structure.

    Args:
        config: Parsed configuration dictionary

    Raises:
        ConfigurationError: If structure is invalid
    """
    if not isinstance(config, dict):
        raise ConfigurationError("Configuration must be a YAML dictionary")

    if "plugins" not in config:
        raise ConfigurationError(
            "Configuration missing 'plugins' section. "
            "See config.yaml template for required structure."
        )

    if not isinstance(config.get("plugins"), dict):
        raise ConfigurationError("'plugins' section must be a dictionary")


# Browser origins allowed to call the MCP route cross-origin when a
# deployment does not set `server_allowed_origins`. Native MCP clients
# send no Origin header at all, so this list never affects them.
DEFAULT_ALLOWED_ORIGINS: Tuple[str, ...] = (
    "https://claude.ai",
    "https://claude.com",
    "https://console.anthropic.com",
    "http://localhost:6274",
    "http://127.0.0.1:6274",
)

# Request paths that carry MCP JSON-RPC when a deployment does not set
# `server_mcp_paths`.
DEFAULT_MCP_PATHS: Tuple[str, ...] = ("/mcp",)


def _string_list(config: Dict[str, Any], key: str) -> List[str]:
    """Read an optional non-empty list of non-empty strings from config."""
    value = config.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ConfigurationError(
            f"'{key}' must be a non-empty list of strings, got {value!r}"
        )
    return [item.strip() for item in value]


def get_http_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the per-deployment HTTP settings, applying defaults.

    Two optional top-level config keys:

    * ``server_allowed_origins`` -- the COMPLETE browser-origin allowlist
      (it replaces the default list rather than extending it, so what is
      allowed is exactly what the config says). The first entry is the
      origin sent on CORS responses to callers with no allowlisted
      Origin.
    * ``server_mcp_paths`` -- the request paths served as MCP routes.
      A deployment exposing a second route on the same handler (for
      example one that API Gateway guards with an API key) lists it
      here.

    Args:
        config: Parsed configuration dictionary

    Returns:
        Dict with ``allowed_origins`` (frozenset), ``default_cors_origin``
        (str) and ``mcp_paths`` (tuple, in configured order)

    Raises:
        ConfigurationError: If either key is present but malformed
    """
    if not isinstance(config, dict):
        raise ConfigurationError("Configuration must be a YAML dictionary")

    if config.get("server_allowed_origins") is None:
        origins = list(DEFAULT_ALLOWED_ORIGINS)
    else:
        origins = _string_list(config, "server_allowed_origins")
        for origin in origins:
            if not origin.startswith(("http://", "https://")) or origin.endswith(
                "/"
            ):
                raise ConfigurationError(
                    "'server_allowed_origins' entries must be origins such "
                    f"as 'https://example.org' (scheme, no trailing slash), "
                    f"got {origin!r}"
                )

    if config.get("server_mcp_paths") is None:
        paths = list(DEFAULT_MCP_PATHS)
    else:
        paths = _string_list(config, "server_mcp_paths")
        for path in paths:
            if not path.startswith("/"):
                raise ConfigurationError(
                    f"'server_mcp_paths' entries must start with '/', "
                    f"got {path!r}"
                )

    return {
        "allowed_origins": frozenset(origins),
        "default_cors_origin": origins[0],
        "mcp_paths": tuple(dict.fromkeys(paths)),
    }


def load_and_validate_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load and validate configuration from YAML file.

    Args:
        config_path: Path to config.yaml file

    Returns:
        Validated configuration dictionary

    Raises:
        ConfigurationError: If validation fails
        FileNotFoundError: If config file doesn't exist
    """
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Create config.yaml based on the template in the repository."
        )
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in {config_path}: {e}")

    if config is None:
        raise ConfigurationError(f"Configuration file {config_path} is empty")

    # Validate structure
    validate_config_structure(config)

    # Validate plugin count (CRITICAL)
    enabled_plugins, count = validate_plugin_count(config)

    # Fail at load time, not on the first request, if the optional HTTP
    # settings are malformed.
    get_http_settings(config)

    logger.info(
        f"Configuration validated: {count} plugin(s) enabled: {', '.join(enabled_plugins)}"
    )

    return config


def get_logging_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Get logging configuration from config dictionary.

    Args:
        config: Parsed configuration dictionary

    Returns:
        Dictionary with logging configuration (level, format)
    """
    logging_config = config.get("logging", {})
    return {
        "level": logging_config.get("level", "INFO"),
        "format": logging_config.get("format", "json"),
    }


def get_enabled_plugin_config(config: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Get the configuration for the single enabled plugin.

    Args:
        config: Parsed configuration dictionary

    Returns:
        Tuple of (plugin_name, plugin_config)

    Raises:
        ConfigurationError: If validation fails
    """
    enabled_plugins, _ = validate_plugin_count(config)

    if len(enabled_plugins) != 1:
        # This should never happen if validate_plugin_count was called first
        raise ConfigurationError("Internal error: Expected exactly one enabled plugin")

    plugin_name = enabled_plugins[0]
    plugin_config = config["plugins"][plugin_name]

    return plugin_name, plugin_config
