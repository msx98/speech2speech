# Book conversation with interruptions

All values below are milliseconds. User/assistant audio is synthetic.

| Step | User speech end → audible reply | Speech end → LLM first token | LLM first token → TTS first voiced output | Speech start → ASR first token | Speech end → ASR last token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 1952 | 1174 | 706 | 4451 | 961 |
| 2 | 1831 | 997 | 743 | 1034 | 806 |
| 3 | 2090 | 1293 | 705 | 2187 | 877 |
| 4 | 1883 | 1253 | 549 | 2808 | 906 |
| 5 | 1996 | 1218 | 706 | 2709 | 886 |
| 6 | 1793 | 1204 | 508 | 1185 | 837 |

| Interruption | Requested seconds into speech | Actual seconds into speech | User onset → playback flush (ms) | User onset → last audible assistant sample (ms) | User onset → server cancel acknowledgement (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2 | 1.0 | 1.002 | 20 | 20 | 20 |
| 5 | 2.0 | 2.002 | 10 | 10 | 11 |

The final seventh step is listening through the last reply, with no new speech.

- ASR is batch inference after end-of-turn detection. First/last token timings observe actual Transformers streamer emissions, including language and control tokens; they are not transcript-return timestamps.
- Server stage intervals use VAD packet boundaries (20 ms input packets). Audible playback uses 10 ms RMS bounds at 0.003. These are estimates rather than acoustic microphone/speaker measurements.
- The LLM first token is observed on upstream SSE (including reasoning tokens when present). TTS first voiced output is the first generated PCM chunk with speech-level energy, before client playback.
- The benchmark client cancels generation and flushes buffered playback when the server detects the interrupting speech. Current browser calls pause microphone input during replies, so this is not a browser barge-in test.
- The server currently treats conversation.item.truncate as a no-op; playback truncation does not guarantee that stored assistant context matches only the heard prefix.
- A negative last-audible-sample interval means the assistant was already silent at the user onset. It is not negative cancellation latency.

[Conversation recording](conversation.wav)
