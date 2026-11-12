# llama.cpp incremental input and speculative responses

`llama-streaming-input.patch` contains only the five server source/documentation
files needed by the sound server's native streaming LLM path. It adds text-only
realtime sessions, chat IDs, immediate incremental prefill, revision corrections,
batch telemetry, and speculative response acceptance/discard with checkpoint rollback.
It excludes local agent notes, model presets, and unrelated research.

Base llama.cpp commit: `44cc07c88750242a02ae78087bb37456337b766b`.

From a clean llama.cpp checkout at that commit:

```sh
git apply --check /path/to/sound-server/src/patches/llama-streaming-input.patch
git apply /path/to/sound-server/src/patches/llama-streaming-input.patch
```

Build llama.cpp normally afterward. The deployed rocbox binary already includes
these changes. The patch passed a clean-base index application check and a reverse
application check against the implemented files. GPU acceptance is in
`../tests/benchmarks/streaming_input.py`; run it against the built server.
