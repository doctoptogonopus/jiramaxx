# Bundled Whisper model

This directory holds the pre-converted faster-whisper `base.en` model that is
bundled into the `jiramaxx-recording` wheel so recording works fully offline.

It is intentionally **empty in source control** (the model is ~145 MB). Populate
it before building the package:

```
python scripts/fetch_model.py
```

That downloads `Systran/faster-whisper-base.en` into `base.en/` (model.bin,
config.json, tokenizer.json, vocabulary.txt). At runtime the engine loads from
`base.en/` with `local_files_only=True` and `HF_HUB_OFFLINE=1`, so end-user
machines make **no network calls** for transcription.

If `base.en/` is absent at runtime (e.g. a dev install that skipped the fetch),
the engine falls back to fetching `base.en` from HuggingFace on first use.
