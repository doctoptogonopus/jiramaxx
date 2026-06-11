"""The app must run fully offline except for Jira/Atlassian traffic.

These tests pin the audited network surface: every HTTP-capable import lives in
api.py, and no module hard-codes a non-Atlassian URL. A new dependency or
feature that widens the surface fails here instead of slipping through review.
"""
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / 'jiramaxx'
MODULES = sorted(PKG.glob('*.py'))

_NET_IMPORT = re.compile(
    r'^\s*(?:import|from)\s+(?:requests|urllib|http\b|http\.client|socket|'
    r'aiohttp|httpx|websocket)', re.MULTILINE)
_URL = re.compile(r'https?://[\w.\-:/]+')

# The only hosts the app may ever reference. Real request URLs all derive from
# the configured base inside api.py; these literals are defaults/help text.
_ALLOWED_URL_PARTS = (
    'atlassian.net',        # DEFAULT_CONFIG placeholder + api.py base handling
    'api.atlassian.com',    # scoped-token gateway (api.py)
    'proxy.corp',           # config UI help-text example, never requested
)


def test_only_api_module_imports_network_libs():
    offenders = [p.name for p in MODULES if p.name != 'api.py'
                 and _NET_IMPORT.search(p.read_text(encoding='utf-8'))]
    assert offenders == [], (
        f'network-capable imports outside api.py: {offenders} — all HTTP must '
        f'go through JiraClient so the offline guarantee holds')


def test_no_foreign_urls_anywhere():
    offenders = []
    for p in MODULES:
        for m in _URL.finditer(p.read_text(encoding='utf-8')):
            if not any(part in m.group(0) for part in _ALLOWED_URL_PARTS):
                offenders.append(f'{p.name}: {m.group(0)}')
    assert offenders == [], f'non-Atlassian URLs found: {offenders}'
