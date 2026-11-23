"""Resolve the asset defaults from a component source tree or a built wheel."""

from pathlib import Path

from ..package_layout import is_source_layout


_ASSETS = Path(__file__).resolve().parent
_COMPONENT = _ASSETS.parents[1]


def asset_config_path(name):
    """Select a layout before reading, so a missing default cannot trigger fallback."""
    if is_source_layout(_COMPONENT):
        return _COMPONENT / "config" / name
    return _ASSETS / "_runtime_config" / name
