"""Static health checks the unit tests can't provide.

- Import smoke test: every module loads (catches bad module-level imports).
- pyflakes: catches *undefined names used inside functions*, which neither
  py_compile nor importing can see — they only surface as NameError at runtime.
"""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = ['jiramaxx.api', 'jiramaxx.cache', 'jiramaxx.config_ui',
           'jiramaxx.main', 'jiramaxx.models', 'jiramaxx.planner',
           'jiramaxx.plugins', 'jiramaxx.ui', 'jiramaxx.utils']

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mod', MODULES)
def test_module_imports(mod):
    importlib.import_module(mod)


def test_pyflakes_clean():
    r = subprocess.run([sys.executable, '-m', 'pyflakes', 'jiramaxx'],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, f'pyflakes findings:\n{r.stdout}{r.stderr}'
