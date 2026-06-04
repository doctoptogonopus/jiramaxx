"""
Developer/build helper: pre-download the faster-whisper `base.en` model into
`jiramaxx_recording/models/base.en/` so it can be bundled into the wheel.

Run ONCE before building the package (not at install time):

    python scripts/fetch_model.py

This requires network access to HuggingFace *at build time only*. End users
never download anything — the model ships inside the wheel and is loaded
offline (see engine._resolve_model_source). The model files are large (~145 MB);
keep them out of normal git history (use git-lfs or fetch-before-build in CI).
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path

DEST = Path(__file__).resolve().parent.parent / 'jiramaxx_recording' / 'models' / 'base.en'


def main() -> int:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is required: pip install huggingface_hub", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Systran/faster-whisper-base.en -> {DEST}")
    path = snapshot_download(
        repo_id='Systran/faster-whisper-base.en',
        allow_patterns=['*.bin', '*.json', '*.txt'],
    )
    for f in Path(path).iterdir():
        if f.is_file():
            shutil.copy2(f, DEST / f.name)
    print("Done. Files:")
    for f in sorted(DEST.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
