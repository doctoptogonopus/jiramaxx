import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jiramaxx.models import init_jira_config, init_ticket_config


@pytest.fixture(autouse=True)
def _reset_model_registries():
    """Each test starts from the hardcoded model defaults; tests that need
    config call init_*_config themselves."""
    init_ticket_config({})
    init_jira_config({})
    yield
    init_ticket_config({})
    init_jira_config({})
