# Client batch statistics acceptance

Test: `node src/tests/benchmarks/browser_call.mjs --speech`, against the rocbox
sound server and native llama server at rocbox:18080. Browser input was the
persistently cached spoken “Thanks!” clip, followed by PCM silence. The model
was instructed to answer with “Hello.” for a short, repeatable smoke test.

Observed single-sample intervals from receipt of the completed prefill batch:

| Milestone | Delay |
| --- | ---: |
| First text chunk ready | 76 ms |
| First decoded audio notification | 358 ms |
| First PCM AudioBuffer ready in browser | 361 ms |

One earlier speculative candidate was discarded and marked separately. These
measurements use the browser's monotonic clock and include event transport and
dispatch. They are not exact GPU execution times or acoustic playback timings.

Browser checks passed for the three graph series, slider range 0–1000 ms and
25 ms steps, held/replaced/continued audio, stale continuation rejection,
changing the delay during a call, zero-delay queue release, and preference
persistence after reload. Node checks cover association when response creation
precedes its final prefill acknowledgement, first-chunk deduplication, averages,
chat isolation, discarded candidates, bounded history and call reset.
