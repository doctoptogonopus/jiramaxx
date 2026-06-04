"""
Plugin host.

Core defines extension points; separately-installed distributions (e.g.
``jiramaxx-recording``) plug into them. Core never imports plugins directly —
it discovers them at runtime via the ``jiramaxx.plugins`` entry-point group.
If no plugin is installed, every hook below is simply never called and the UI
renders without the plugin's contributions.

A plugin registers itself in its own ``pyproject.toml``::

    [tool.poetry.plugins."jiramaxx.plugins"]
    recording = "jiramaxx_recording.plugin:RecordingPlugin"

All hooks are optional; subclass :class:`Plugin` and override what you need.
Hooks must never raise — discovery and dispatch are defensively wrapped so a
broken or partially-installed plugin can never take down the core app.
"""
from __future__ import annotations

from importlib import metadata
from typing import Any


class Plugin:
    """Base class for jiramaxx plugins. Override the hooks you need."""

    #: Short identifier, used for diagnostics only.
    name: str = ''

    # ── Main window ──────────────────────────────────────────────────────────
    def main_buttons(self) -> list:
        """Return PySimpleGUI elements to append to the main window button area."""
        return []

    def handle_main_event(self, event, values, window, ctx: dict) -> bool:
        """Handle a main-window event. Return True if consumed.

        ``ctx`` carries at least ``{'config': <dict>}``.
        """
        return False

    def on_main_window_close(self) -> None:
        """Called when the main window is closing (clean up background work)."""

    # ── Config window ────────────────────────────────────────────────────────
    def config_tab(self, config: dict):
        """Return a ``sg.Tab`` to add to the config window, or None."""
        return None

    def handle_config_event(self, event, values, window, working: dict) -> bool:
        """Handle a config-window event. Return True if consumed."""
        return False

    def collect_config(self, values, working: dict) -> None:
        """Write this plugin's settings into ``working`` just before it is saved."""


_PLUGINS: list[Plugin] | None = None


def discover_plugins() -> list[Plugin]:
    """Discover and instantiate installed plugins (memoized).

    Never raises: a plugin that fails to load or instantiate is skipped.
    """
    global _PLUGINS
    if _PLUGINS is not None:
        return _PLUGINS

    found: list[Plugin] = []
    try:
        eps = metadata.entry_points(group='jiramaxx.plugins')
    except Exception:
        eps = ()
    for ep in eps:
        try:
            obj: Any = ep.load()
            inst = obj() if isinstance(obj, type) else obj
            if isinstance(inst, Plugin):
                if not inst.name:
                    inst.name = ep.name
                found.append(inst)
        except Exception:
            # A broken/partially-installed plugin must never crash core.
            continue

    _PLUGINS = found
    return _PLUGINS
