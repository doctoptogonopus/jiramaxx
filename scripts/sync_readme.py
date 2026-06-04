#!/usr/bin/env python3
"""Keep the repo-root README.md in sync with the canonical packaged README.

`jiramaxx/README.md` is the source of truth — it's the README referenced by
pyproject.toml and shipped in the wheel. The repo-root `README.md` exists only
because GitHub renders it as the landing page, so it must mirror the packaged
one exactly.

**Edit `jiramaxx/README.md`.** This script (run from the pre-commit hook) copies
it to `./README.md` and stages the result, so the two never drift. Run manually
any time with:  python scripts/sync_readme.py
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANONICAL = REPO / 'jiramaxx' / 'README.md'
MIRROR = REPO / 'README.md'


def main() -> int:
    if not CANONICAL.exists():
        return 0  # nothing to sync
    new = CANONICAL.read_bytes()
    if MIRROR.exists() and MIRROR.read_bytes() == new:
        return 0  # already in sync
    MIRROR.write_bytes(new)
    subprocess.run(['git', 'add', str(MIRROR)], cwd=REPO, check=False)
    print('pre-commit: synced README.md from jiramaxx/README.md '
          '(edit jiramaxx/README.md, not the root copy)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
