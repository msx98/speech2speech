# Cached speaker inputs

Six prerecorded English inputs for `realtime_interruptions.py`. See
`manifest.json` for their order and exact prompts. Each clip has a JSON
sidecar with its TTS request, SHA-256, duration, and ASR verification.
The benchmark reuses these files without new TTS/ASR calls when their
checksums and request settings match.

Generated with Qwen3-TTS 1.7B CustomVoice and the built-in Ryan speaker.
"wait..." is synthesized as "Wait." to avoid Qwen vocalizing the ellipsis
as extra filler. It contains only the requested spoken word. Quiet
consonants are protected by 120 ms of padding around the speech bounds.

These are synthetic inputs. The last step in the scenario is listening
through the final assistant reply, so there is no seventh speech clip.
