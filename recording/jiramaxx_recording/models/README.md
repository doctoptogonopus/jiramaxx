# Local Whisper model (optional, for air-gapped builds)

By default this directory is **empty** and the model is **not** bundled into the
wheel — the engine downloads `base.en` (~145 MB) from Hugging Face on first use,
then runs offline. This keeps the wheel small and publishable on public PyPI.

For a fully air-gapped build, populate `base.en/` before building:

```
python scripts/fetch_model.py
```

That downloads `Systran/faster-whisper-base.en` into `base.en/` (model.bin,
config.json, tokenizer.json, vocabulary.txt). When `base.en/` is present, the
engine loads from it directly with `local_files_only=True` and `HF_HUB_OFFLINE=1`,
making **zero network calls**. To actually ship it in the wheel, also add an
`include` for `jiramaxx_recording/models/**/*` in `pyproject.toml`.
