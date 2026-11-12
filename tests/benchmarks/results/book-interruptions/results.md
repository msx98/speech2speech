# Book conversation with interruptions

All values below are milliseconds. User/assistant audio is synthetic.

| Step | User speech end → audible reply | Speech end → LLM first token | LLM first token → TTS first voiced output | Speech start → ASR first token | Speech end → ASR last token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 1720 | 929 | 719 | 727 | 435 |
| 2 | 1518 | 882 | 555 | 500 | 270 |
| 3 | 1970 | 1217 | 662 | 506 | 803 |
| 4 | 1705 | 1033 | 590 | 507 | 223 |
| 5 | 1777 | 1001 | 704 | 501 | 289 |
| 6 | 1643 | 1042 | 519 | 508 | 157 |

| Interruption | Requested seconds into speech | Actual seconds into speech | User onset → playback flush (ms) | User onset → last audible assistant sample (ms) | User onset → server cancel acknowledgement (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2 | 1.0 | 1.003 | 20 | 20 | 20 |
| 5 | 2.0 | 2.001 | 11 | 11 | 11 |

The final seventh step is listening through the last reply, with no new speech.

- ASR uses progressive rolling-prefix recognition by default (batch mode with --batch-asr). Partial hypotheses may be revised. First/last token timings observe actual Transformers streamer emissions across prefix decodes, including language and control tokens; they are not transcript-return timestamps.
- Server stage intervals use VAD packet boundaries (20 ms input packets). Audible playback uses 10 ms RMS bounds at 0.003. These are estimates rather than acoustic microphone/speaker measurements.
- The LLM first token is observed on upstream SSE (including reasoning tokens when present). TTS first voiced output is the first generated PCM chunk with speech-level energy, before client playback.
- The benchmark client cancels generation and flushes buffered playback when the server detects the interrupting speech. Current browser calls pause microphone input during replies, so this is not a browser barge-in test.
- The server currently treats conversation.item.truncate as a no-op; playback truncation does not guarantee that stored assistant context matches only the heard prefix.
- A negative last-audible-sample interval means the assistant was already silent at the user onset. It is not negative cancellation latency.

[Conversation recording](conversation.wav)
