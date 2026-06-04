# jiramaxx-recording

Optional meeting-recording + local transcription plugin for
[jiramaxx](https://github.com/doctoptogonopus/jiramaxx).

This is a **separate distribution** from core `jiramaxx`. It is intended for
environments where audio recording is permitted; corporate sites that disallow
recording simply omit it from their package mirror, and core `jiramaxx` keeps
working with no Record button or Recording tab.

## Install

```
pip install jiramaxx-recording        # explicit
pip install jiramaxx[recording]       # convenience alias for the same thing
```

There is **no separate command** — the app is still launched with `jiramaxx`.
Once this package is installed, core discovers it via the `jiramaxx.plugins`
entry point and adds a Record button (main window) and a Recording tab (config).

## Choosing / downloading the model

Open **Config → Recording → Transcription**:

- **Model size** — pick `tiny.en` / `base.en` (default) / `small.en` /
  `medium.en` / `large-v3`. Larger = more accurate but slower and a bigger
  download.
- **Download / Prepare model** — explicitly downloads the selected model from
  Hugging Face (with a confirmation prompt) and loads it, so recording starts
  instantly afterward. **Nothing is downloaded automatically** — only when you
  click this button, or when you confirm the prompt the first time you hit
  Record. If you never do either, nothing is ever fetched.
- **Custom model path** (optional) — point at a local CTranslate2 Whisper model
  directory. This **overrides** the size picker and runs fully offline (no
  download), e.g. for air-gapped installs.

When you click **Record** and the chosen model isn't downloaded yet, you're asked
to confirm the download; recording does **not** start until it completes, so the
download never competes with audio capture.

## Offline / privacy

Transcription runs entirely on the local CPU via faster-whisper — **no audio or
text ever leaves the machine**. The only network activity is the one-time,
read-only download of the public Whisper weights described above; after that the
cached model loads offline.

For a fully air-gapped install, set a **Custom model path**, or pre-fetch the
default with `scripts/fetch_model.py` into `jiramaxx_recording/models/base.en/`
(loaded directly with no download).

## Disabling via policy

Set `JIRAMAXX_DISABLE_RECORDING=1` (or `true`/`yes`/`on`) in the environment to
force-disable recording even when this package is installed. The Record button
is disabled and the Recording tab shows a policy message.
