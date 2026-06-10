"""
Entry point. Runs as a background listener that registers global hotkeys and
opens the GUI on demand. The keyboard library fires callbacks on a background
thread; we use a queue to marshal GUI work back to the main thread (required
by tkinter / PySimpleGUI).

Usage:
    python main.py           # background listener mode
    python main.py --gui     # open GUI directly (skip hotkey daemon)
"""
from __future__ import annotations
import copy
import queue
import shutil
import sys
import threading
from pathlib import Path

import yaml
import PySimpleGUI as sg
import keyboard

from .api import JiraClient, apply_proxy_env, resolve_token
from .cache import Cache
from .models import init_ticket_config, init_jira_config
from .ui import run_main_window, show_interaction_window

# Config lives in the user's home directory, not inside the installed package —
# site-packages is often read-only (and shared) for pip installs, and credentials
# do not belong there.
CONFIG_PATH = Path.home() / '.jiramaxx' / 'config.yaml'
# Old scattered drafts location, retired in favor of one folder under base_dir.
LEGACY_CACHE_DIR = Path.home() / '.jira_tool' / 'cache'

DEFAULT_CONFIG: dict = {
    'jira': {
        'base_url': 'https://yourcompany.atlassian.net',
        'api_token': '',
        'user_email': '',
        'project_key': 'ENG',
        'token_type': 'classic',
        'cloud_id': '',
    },
    # One folder holds config.yaml, drafts, and sprint snapshots so users can move
    # or clear everything at once. Drafts → <base_dir>/drafts, sprint snapshots →
    # <base_dir>/drafts/sprints, config.yaml stays at ~/.jiramaxx/config.yaml.
    'paths': {'base_dir': '~/.jiramaxx'},
    'ui': {'theme': 'DarkBlue3'},
    'hotkeys': {
        'create_ticket': 'ctrl+alt+j',
        'manage_tickets': 'ctrl+alt+m',
    },
    # In-window key bindings (apply on next window open). Only the frequent
    # per-ticket actions get a shortcut; view options live behind buttons.
    'shortcuts': {
        'comment': 'c',
        'status': 's',
        'subtask': 't',
        'update': 'u',
    },
    # Release mode: the status it filters to, and the status it bulk-moves to.
    # Configurable so it adapts to different workflows.
    'release': {
        'filter_status': 'Ready for Release',
        'done_status': 'Done',
        # Set true only by Config → Release settings → "Test statuses"; Release mode
        # stays disabled until both statuses are confirmed to exist in the project.
        'validated': False,
    },
    'network': {
        'use_system_certs': True,
        'ca_bundle': '',
        'proxy': '',
    },
}


def data_dir(config: dict) -> Path:
    """Resolve the drafts directory. Honors an explicit, non-legacy
    ``cache.directory`` for back-compat; otherwise derives ``<base_dir>/drafts``."""
    explicit = (config.get('cache') or {}).get('directory')
    if explicit:
        p = Path(explicit).expanduser()
        if p != LEGACY_CACHE_DIR:
            return p
    base = (config.get('paths') or {}).get('base_dir') or '~/.jiramaxx'
    return Path(base).expanduser() / 'drafts'


def migrate_legacy_cache(target: Path) -> None:
    """One-time relocation of old ~/.jira_tool/cache drafts into the new folder."""
    if target.exists() or not LEGACY_CACHE_DIR.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_CACHE_DIR), str(target))
    except Exception:
        pass


def enable_system_certs(config: dict) -> None:
    """Verify TLS against the OS trust store so corporate root CAs that IT
    installed system-wide are honored (requests' bundled certifi list ignores
    the Windows store). Defensive: any failure falls back to default behavior,
    so home users are unaffected. Honors network.use_system_certs to opt out."""
    if not config.get('network', {}).get('use_system_certs', True):
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass


def _merge_defaults(data: dict, defaults: dict) -> dict:
    """Deep-merge ``data`` over ``defaults``: every default key is present in the
    result, user values win, nested dicts merge recursively. Defaults are copied,
    never aliased, so the result is safe to mutate."""
    out = copy.deepcopy(defaults)
    for k, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_defaults(v, out[k])
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """Load ``~/.jiramaxx/config.yaml`` merged over DEFAULT_CONFIG. An empty,
    partial, or mangled file is a logical case, not an error: it merges to the
    defaults, so startup always reaches the normal setup prompt."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, allow_unicode=True)
        return copy.deepcopy(DEFAULT_CONFIG)
    with open(CONFIG_PATH, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return _merge_defaults(data if isinstance(data, dict) else {}, DEFAULT_CONFIG)


def is_configured(config: dict) -> bool:
    return bool(resolve_token(config.get('jira', {})))


def _prompt_setup(config: dict) -> dict:
    """Open config UI for first-time setup. Returns updated config or exits."""
    from .config_ui import show_config_window
    sg.popup(
        'No API token found.\n\nFill in your Jira credentials to get started.',
        title='Setup Required', modal=True, keep_on_top=True,
    )
    updated = show_config_window(config, CONFIG_PATH)
    if updated and is_configured(updated):
        return updated
    sg.popup('An API token is required. Exiting.', title='Setup Required',
             modal=True, keep_on_top=True)
    sys.exit(0)


def build_clients(config: dict) -> tuple[Cache, JiraClient]:
    ddir = data_dir(config)
    migrate_legacy_cache(ddir)
    cache = Cache(str(ddir))
    jira = JiraClient.from_config(config)
    return cache, jira


def _bootstrap() -> dict:
    """Shared startup: load config, wire TLS/proxy/theme, init model registries,
    and run first-time setup if no token is configured."""
    config = load_config()
    enable_system_certs(config)
    apply_proxy_env(config.get('network', {}))
    sg.theme(config.get('ui', {}).get('theme', 'DarkBlue3'))
    init_ticket_config(config.get('ticket_types', {}))
    init_jira_config(config.get('jira', {}))
    if not is_configured(config):
        config = _prompt_setup(config)
        init_ticket_config(config.get('ticket_types', {}))
        init_jira_config(config.get('jira', {}))
    return config


def main():
    config = _bootstrap()
    cache, jira = build_clients(config)

    if '--gui' in sys.argv:
        run_main_window(cache, jira, config, CONFIG_PATH)
        return

    hotkeys = config['hotkeys']  # always present after the defaults merge
    gui_queue: queue.Queue[str] = queue.Queue()
    gui_busy = threading.Lock()

    keyboard.add_hotkey(hotkeys['create_ticket'],  lambda: gui_queue.put('main'))
    keyboard.add_hotkey(hotkeys['manage_tickets'], lambda: gui_queue.put('manage'))

    print(f"[JIRAMAXXING]. {hotkeys['create_ticket']} = new/drafts  |  "
          f"{hotkeys['manage_tickets']} = manage  |  Ctrl-C = quit")

    # Main thread: poll queue and dispatch GUI (tkinter must run on main thread).
    try:
        while True:
            try:
                action = gui_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if not gui_busy.acquire(blocking=False):
                continue  # GUI already open; swallow the hotkey press

            try:
                if action == 'main':
                    run_main_window(cache, jira, config, CONFIG_PATH)
                elif action == 'manage':
                    show_interaction_window(cache, jira, config)
            finally:
                gui_busy.release()

    except KeyboardInterrupt:
        print("\n[JIRAMINIMIZING].")


if __name__ == '__main__':
    main()
