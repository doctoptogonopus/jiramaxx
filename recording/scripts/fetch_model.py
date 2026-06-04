"""
OPTIONAL helper for air-gapped/offline builds: pre-download the faster-whisper
`base.en` model into `jiramaxx_recording/models/base.en/`.

The default build does NOT use this — end users download the model from
HuggingFace on first use and run offline thereafter (see
engine._resolve_model_source). Run this only when you want a self-contained
offline install:

    python scripts/fetch_model.py

It downloads `base.en` (~145 MB) so the engine can load it directly with no
network calls. To actually ship it inside the wheel, also add an `include` for
`jiramaxx_recording/models/**/*` to pyproject.toml before building. The model
files are large; keep them out of normal git history (git-lfs or fetch-in-CI).
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
