# Book conversation with interruptions

All values below are milliseconds. User/assistant audio is synthetic.

| Step | User speech end → audible reply | Speech end → LLM first token | LLM first token → TTS first voiced output | Speech start → ASR first token | Speech end → ASR last token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 1645 | 759 | 814 | 726 | 384 |
| 2 | 1475 | 759 | 634 | 500 | 276 |
| 3 | 1723 | 824 | 808 | 503 | 764 |
| 4 | 1431 | 760 | 590 | 506 | 216 |
| 5 | 1391 | 760 | 559 | 500 | 253 |
| 6 | 1489 | 760 | 647 | 505 | 154 |

| Interruption | Requested seconds into speech | Actual seconds into speech | User onset → playback flush (ms) | User onset → last audible assistant sample (ms) | User onset → server cancel acknowledgement (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2 | 1.0 | 1.001 | 20 | 20 | 21 |
| 5 | 2.0 | 2.001 | 11 | 11 | 12 |

The final seventh step is listening through the last reply, with no new speech.

- ASR uses progressive rolling-prefix recognition by default (batch mode with --batch-asr). Partial hypotheses may be revised. First/last token timings observe actual Transformers streamer emissions across prefix decodes, including language and control tokens; they are not transcript-return timestamps.
- Server stage intervals use VAD packet boundaries (20 ms input packets). Audible playback uses 10 ms RMS bounds at 0.003. These are estimates rather than acoustic microphone/speaker measurements.
- The LLM first token is observed on upstream SSE (including reasoning tokens when present). TTS first voiced output is the first generated PCM chunk with speech-level energy, before client playback.
- The benchmark client cancels generation and flushes buffered playback when the server detects the interrupting speech. Current browser calls pause microphone input during replies, so this is not a browser barge-in test.
- The server currently treats conversation.item.truncate as a no-op; playback truncation does not guarantee that stored assistant context matches only the heard prefix.
- A negative last-audible-sample interval means the assistant was already silent at the user onset. It is not negative cancellation latency.

[Conversation recording](conversation.wav)

## Incremental LLM input

| Step | ASR text updates sent | Prefill batches received before user speech ended |
| --- | ---: | ---: |
| 1 | 14 | 17 |
| 2 | 2 | 0 |
| 3 | 6 | 4 |
| 4 | 9 | 7 |
| 5 | 9 | 9 |
| 6 | 2 | 0 |
