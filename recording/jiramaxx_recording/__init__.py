"""jiramaxx-recording: optional meeting-recording plugin for jiramaxx.

Installed separately from core (``pip install jiramaxx-recording`` or
``pip install jiramaxx[recording]``). It plugs into the core app via the
``jiramaxx.plugins`` entry point and contributes a Record button + Recording
config tab. Core never imports this package directly.
"""
__version__ = "0.1.0"
