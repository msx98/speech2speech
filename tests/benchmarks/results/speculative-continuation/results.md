# Book conversation with interruptions

All values below are milliseconds. User/assistant audio is synthetic.

| Step | User speech end → audible reply | Speech end → LLM first token | LLM first token → TTS first voiced output | Speech start → ASR first token | Speech end → ASR last token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 2270 | 1664 | 524 | 3570 | 1605 |

| Interruption | Requested seconds into speech | Actual seconds into speech | User onset → playback flush (ms) | User onset → last audible assistant sample (ms) | User onset → server cancel acknowledgement (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |

The final seventh step is listening through the last reply, with no new speech.

- ASR uses progressive rolling-prefix recognition by default (batch mode with --batch-asr). Partial hypotheses may be revised. First/last token timings observe actual Transformers streamer emissions across prefix decodes, including language and control tokens; they are not transcript-return timestamps.
- Server stage intervals use VAD packet boundaries (20 ms input packets). Audible playback uses 10 ms RMS bounds at 0.003. These are estimates rather than acoustic microphone/speaker measurements.
- The LLM first token is observed on upstream SSE (including reasoning tokens when present). TTS first voiced output is the first generated PCM chunk with speech-level energy, before client playback.
- The benchmark buffers provisional audio until server commitment and flushes playback on detected interruptions. The browser independently gates playback using local speech detection and the configured silence gap; its microphone remains active during replies. This synthetic benchmark is not an acoustic browser barge-in test.
- The server currently treats conversation.item.truncate as a no-op; playback truncation does not guarantee that stored assistant context matches only the heard prefix.
- A negative last-audible-sample interval means the assistant was already silent at the user onset. It is not negative cancellation latency.

[Conversation recording](conversation.wav)

## Incremental LLM input

| Step | ASR text updates sent | Prefill batches received before user speech ended |
| --- | ---: | ---: |
| 1 | 21 | 19 |
