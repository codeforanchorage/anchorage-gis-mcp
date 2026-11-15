"""The shared GIS plugin base is a dependency (opencontext-arcgis-hub).

Same seam checks as test_core_dependency.py: the package must be
importable, this repo's plugin must be built on it, and it must be pinned
to the same version everywhere it is declared.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"opencontext-arcgis-hub @ git\+https://\S+?@(v[\d.]+)")


def test_hub_is_not_vendored():
    """A stray arcgis_hub/ directory here would shadow the package."""
    assert not (REPO_ROOT / "arcgis_hub").exists()


def test_enabled_plugin_is_built_on_the_hub(monkeypatch):
    from arcgis_hub.plugin import ArcGISHubPlugin
    from core.plugin_manager import PluginManager
    from core.validators import get_enabled_plugin_config

    monkeypatch.chdir(REPO_ROOT)
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text("utf-8"))
    enabled, _ = get_enabled_plugin_config(config)
    manager = PluginManager(config)
    path = dict(manager.discover_plugins())[enabled]
    plugin_class = manager._load_plugin_class(enabled, path)

    # The loader must pick this repo's subclass, never the imported base.
    assert plugin_class is not ArcGISHubPlugin
    assert issubclass(plugin_class, ArcGISHubPlugin)
    assert plugin_class.plugin_name == enabled


def test_installed_hub_matches_the_pin():
    """A stale environment would otherwise test one hub and ship
    another: the pin is what deploy.sh installs."""
    import importlib.metadata

    pinned = PIN.findall((REPO_ROOT / "requirements.txt").read_text("utf-8"))
    assert ["v" + importlib.metadata.version("opencontext-arcgis-hub")] == pinned


def test_hub_pin_is_the_same_everywhere():
    pins = {
        name: PIN.findall((REPO_ROOT / name).read_text("utf-8"))
        for name in ("requirements.txt", "pyproject.toml")
    }
    assert all(len(found) == 1 for found in pins.values()), pins
    assert len({found[0] for found in pins.values()}) == 1, pins
