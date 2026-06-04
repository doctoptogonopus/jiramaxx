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

## Offline / privacy

Transcription runs entirely on the local CPU via faster-whisper — **no audio or
text ever leaves the machine**. The Whisper model is bundled inside the wheel
and loaded with `local_files_only` + `HF_HUB_OFFLINE`, so there are **no network
calls at runtime**. (See `scripts/fetch_model.py` for how the model is fetched
at *build* time.)

## Disabling via policy

Set `JIRAMAXX_DISABLE_RECORDING=1` (or `true`/`yes`/`on`) in the environment to
force-disable recording even when this package is installed. The Record button
is disabled and the Recording tab shows a policy message.
