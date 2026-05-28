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
import queue
import sys
import threading
from pathlib import Path

import yaml
import PySimpleGUI as sg
import keyboard

from .api import JiraClient
from .cache import Cache
from .models import init_ticket_config, init_jira_config
from .ui import run_main_window, show_interaction_window

CONFIG_PATH = Path(__file__).parent / 'config.yaml'

DEFAULT_CONFIG: dict = {
    'jira': {
        'base_url': 'https://yourcompany.atlassian.net',
        'api_token': '',
        'user_email': '',
        'project_key': 'ENG',
        'board_id': 1,
        'token_type': 'classic',
        'cloud_id': '',
    },
    'cache': {'directory': '~/.jira_tool/cache'},
    'ui': {'theme': 'DarkBlue3'},
    'hotkeys': {
        'create_ticket': 'ctrl+alt+j',
        'manage_tickets': 'ctrl+alt+m',
    },
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def is_configured(config: dict) -> bool:
    return bool((config.get('jira', {}).get('api_token') or '').strip())


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
    cache = Cache(config.get('cache', {}).get('directory', '~/.jira_tool/cache'))
    jcfg = config['jira']
    jira = JiraClient(
        jcfg['base_url'],
        jcfg.get('user_email', ''),
        jcfg.get('api_token', ''),
        token_type=jcfg.get('token_type', 'classic'),
        cloud_id=jcfg.get('cloud_id', ''),
    )
    return cache, jira


def main():
    config = load_config()
    sg.theme(config.get('ui', {}).get('theme', 'DarkBlue3'))
    init_ticket_config(config.get('ticket_types', {}))
    init_jira_config(config.get('jira', {}))
    if not is_configured(config):
        config = _prompt_setup(config)
        init_ticket_config(config.get('ticket_types', {}))
        init_jira_config(config.get('jira', {}))
    cache, jira = build_clients(config)

    hotkeys = config.get('hotkeys', DEFAULT_CONFIG['hotkeys'])
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
                    run_main_window(cache, jira, config)
                elif action == 'manage':
                    show_interaction_window(jira, config)
            finally:
                gui_busy.release()

    except KeyboardInterrupt:
        print("\n[JIRAMINIMIZING].")


if __name__ == '__main__':
    config = load_config()
    sg.theme(config.get('ui', {}).get('theme', 'DarkBlue3'))

    if '--gui' in sys.argv:
        init_ticket_config(config.get('ticket_types', {}))
        init_jira_config(config.get('jira', {}))
        if not is_configured(config):
            config = _prompt_setup(config)
            init_ticket_config(config.get('ticket_types', {}))
            init_jira_config(config.get('jira', {}))
        cache, jira = build_clients(config)
        run_main_window(cache, jira, config, CONFIG_PATH)
    else:
        main()
