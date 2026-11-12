# Realtime call benchmarks

Run the server inside rocbox. These benchmarks perform actual inference.

From the repository root inside rocbox:

```sh
PORT=18000 bash src/run.sh
# In another terminal:
.venv/bin/python src/tests/benchmarks/realtime_conversation.py
```

The conversation benchmark generates three English utterances with Ryan,
streams PCM at speaking speed, prints ASR transcripts, and measures time
from the end of each input to first reply audio. It retains conversation
context across turns. Override the server, LLM, TTS, or voice with
`--base-url`, `--llm`, `--tts`, and `--voice`.
Use `--silence-ms` to vary the end-of-turn threshold and repeat `--phrase`
to supply custom inputs. A negative first-audio gap means the assistant
replied before the speaker finished; it is not a valid latency improvement.

`--record-file /path/to/conversation.wav` optionally records a stereo WAV
with the simulated speaker on the left and assistant on the right. The
recording preserves packet timing, queued playback, and pauses, rather than
concatenating the spoken turns. It simulates client playback; it is not a
physical microphone/speaker recording. Inputs are synthesized before the
session starts, so input preparation does not add artificial conversational
pauses. `--verify-audio` transcribes each reply through ASR.

Use `--output-dir` to save individual input/reply WAVs and `results.json`.
Reuse those inputs with `--input-dir` for comparisons, and use
`--instructions` to request identical replies. `--no-streaming` selects the
old complete-sentence generation path. On rocbox, the checkout is read-only;
write artifacts under `~/.local/share/sound-server/benchmarks/`, then copy
them back into this directory from the desktop.

`browser_call.mjs` uses Chromium's debugging port at localhost:9222 and a
server at localhost:18000. Start a disposable browser profile with
`--remote-debugging-port=9222 --use-fake-ui-for-media-stream
--use-fake-device-for-media-stream --autoplay-policy=no-user-gesture-required`.
Run it with `node src/tests/benchmarks/browser_call.mjs`. It checks call
connection, selected models and voice, mute, response audio, and hang-up.
The synthetic microphone supplies a tone; the reply test uses a text event.
Use the Python benchmark to exercise speech transcription and turn detection.

`browser_record_clip.mjs` tests the Config tab's Record button, saved WAV and
automatic transcription, microphone cleanup, and permission-denial recovery.
Launch Chromium with the same flags above plus
`--use-file-for-fake-audio-capture=/absolute/path/to/fixtures/book-interruptions/01-10da956148cc.wav`,
then run `node src/tests/benchmarks/browser_record_clip.mjs`. It creates a voice
clip through the real upload, transcription, and clip-creation endpoints.

## Measured on rocbox, 2026-11-12

Ryan input, Qwen3.5-4B LLM, Angel profile with OmniVoice replies, 700 ms
silence detection, models warmed before the three-turn test:

| Turn | Input duration | ASR completed from input start | First audio after input end |
| --- | ---: | ---: | ---: |
| 1 | 6.96 s | 8.01 s | 2.31 s |
| 2 | 5.04 s | 6.08 s | 2.19 s |
| 3 | 4.72 s | 5.65 s | 2.04 s |

All three ASR transcripts matched the intended words. Responses followed
the conversation and produced audio. This measures generated speech and
server processing, not human listening quality or physical microphone and
speaker latency. Cold starts can be substantially slower: an earlier short
quiet sample took about 14 seconds to transcribe and was misidentified as
Japanese. The call UI pauses microphone input during assistant replies, so
it does not support spoken interruptions. Roughly two-second turn gaps and
the absence of overlapping speech limit how natural the conversation feels.

## Qwen3-TTS 1.7B CustomVoice comparison

Run without reference audio or cloning:

```sh
.venv/bin/python src/tests/benchmarks/realtime_conversation.py \\
  --tts qwen3-tts-1.7b-customvoice --voice Ryan
```

The first turn produced audio 6.24 seconds after input ended (including
cold ASR startup); the second produced audio after 4.02 seconds with warm
ASR. Both input transcripts were accurate and replies followed context.
On the third input, a pause after “Thank you” triggered the 700 ms VAD
threshold. The assistant responded to that fragment before the rest of the
utterance finished. That turn is an early-reply failure, not a valid latency
sample. This run was slower than the earlier OmniVoice run, though generated
input and reply lengths vary, so it is not a controlled model-speed comparison.
A follow-up single-sentence input (without the internal pause) transcribed
accurately and received reply audio 3.88 seconds after input ended.

Those historical measurements used complete-sentence TTS: the LLM streamed
tokens into sentence/chunk accumulation, but each chunk was synthesized in
full before its PCM was sent.

## Incremental CustomVoice results

Realtime CustomVoice now decodes codec frames during generation in batches
of eight frames, using the tokenizer's 25-frame left context. The temporary
forward hook is removed on completion, failure, and cancellation. Other
backends keep the original synthesis path. `session.update` accepts
`stream_tts` (default true); set it false for the baseline.

Recorded comparison using the same three input WAVs and the exact same
reply text, with warm models:

| Measurement | Complete sentence | Incremental codec output |
| --- | ---: | ---: |
| Average input-end to first audio | 2.80 s | 1.62 s |
| Range across three turns | 2.78–2.81 s | 1.58–1.66 s |
| TTS start to first audio | 1.56–1.63 s | 0.39 s |
| Reply ASR matches intended words | 3/3 | 3/3 |
| Client-timeline buffer underruns over 20 ms | 0 | 0 |

This is about a 42% reduction in first-audio latency for these short replies.
It does not measure human listening quality. Voice generation still takes
sentence/chunk text as input; token-by-token text ingestion is not implemented.
Decoding overlapping windows adds some compute; this optimizes time to first
audio rather than overall synthesis throughput.

Recordings (speaker left, assistant right):

- [Complete-sentence baseline](results/recorded-baseline/conversation.wav)
- [Incremental output comparison](results/recorded-streaming/conversation.wav)
- [Three-turn conversation with varied replies](results/recorded-conversation/conversation.wav)

Raw per-turn measurements and individual WAV files are alongside each
recording. The varied conversation uses a retained session and short replies
to a relaxation question, a follow-up question, and a farewell.
Its three gaps were 1.91, 2.09, and 2.05 seconds. Input and reply ASR matched
the intended words across all turns; no client-timeline buffer underruns
over 20 ms were observed.

Example recording command (from the repository root on rocbox):

```sh
.venv/bin/python src/tests/benchmarks/realtime_conversation.py \\
  --tts qwen3-tts-1.7b-customvoice --voice Ryan --verify-audio \\
  --phrase 'Can you suggest something relaxing to do this afternoon?' \\
  --phrase 'Could you suggest one more activity I can do at home?' \\
  --phrase 'Thanks for those ideas and I hope you have a lovely afternoon.' \\
  --output-dir "$HOME/.local/share/sound-server/benchmarks/conversation" \\
  --record-file "$HOME/.local/share/sound-server/benchmarks/conversation/call.wav"
```

### Timing events

`response.timing` contains `stages` in milliseconds from the last voiced
input packet (or response creation for text-triggered replies):
`turn_committed`, `asr_started`, `asr_completed`, `response_started`,
`llm_first_token`, `first_text_chunk`, `tts_started`, `tts_first_audio`,
`first_audio_sent`, and `response_done`. Cancellation and errors add
`response_cancelled` or `response_failed` instead.

The call UI reports an estimated first playback time from the audio clock
and output latency. The server records `playback_ack_received` and
`browser_receive_to_playback`; the former includes network transit, and the
latter is a browser-side interval rather than an acoustic measurement.
Browser consumers can observe the `studio:call-timing` custom event.

Run `realtime_cancel.py` against the test server to verify cancellation
during codec generation and successful reuse of the model in the same session.
Unit checks for codec boundaries, hook cleanup, and stereo recording are:

```sh
.venv/bin/python -m unittest discover -s src/tests -p 'test_*.py'
```

## Book conversation and timed interruptions

`realtime_interruptions.py` runs the six canned spoken inputs in one session:

1. “Could you suggest a good book to read this afternoon?”
2. Interrupt after 1 second of audible assistant speech: “wait...”
3. Wait through the reply, then “Ok, go on.”
4. Wait through the reply, then “Thanks, do you have another suggestion?”
5. Interrupt after 2 seconds of audible assistant speech: “a fantasy book please.”
6. Wait through the reply, then “Thanks!”
7. Listen through the final reply; send no additional speech.

The six prerecorded inputs are persisted under
[`fixtures/book-interruptions`](fixtures/book-interruptions/README.md), with
checksums, exact TTS requests, and ASR verification. Missing clips are generated
once; matching verified clips are reused without new TTS or verification calls.
Use `--clip-dir` for a different persistent cache. Interruptions are scheduled
from audible assistant onset, compensating for the input clips' quiet padding.
Input packets are transmitted after their simulated microphone collection,
so server detection never sees future audio.

Run inside rocbox from the repository root:

```sh
.venv/bin/python src/tests/benchmarks/realtime_interruptions.py \
  --warmup \
  --output-dir "$HOME/.local/share/sound-server/benchmarks/book-interruptions" \
  --record-file "$HOME/.local/share/sound-server/benchmarks/book-interruptions/conversation.wav"
```

Use `--prepare-only` to prepare a cache without running the conversation.
On a read-only checkout, specify a writable `--clip-dir` when generating new
fixtures, then copy the verified files into the repository from the desktop.
Use `--batch-asr` to compare with the previous ASR path.

The report measures every user turn's speech-end to audible reply, speech-end
to first LLM token, first LLM token to first voiced TTS output, speech-start
to first ASR token, and speech-end to last ASR token. It separately records
the time from an interruption's audible onset to client playback flush, last
audible assistant sample, and server cancellation acknowledgement. First/last
ASR tokens are observed via Transformers' streamer, excluding prompt input
but including generated language/control tokens. LLM timing uses the first
upstream SSE token, including reasoning tokens when present. Audio speech
bounds use 10 ms RMS windows; this is simulated playback, not an acoustic
microphone/speaker measurement.

Recordings and detailed per-turn tables:

- [Progressive-ASR interruption recording](results/book-interruptions/conversation.wav)
- [Progressive-ASR timings](results/book-interruptions/results.md)
- [Batch-ASR baseline recording](results/book-interruptions-batch/conversation.wav)
- [Batch-ASR timings](results/book-interruptions-batch/results.md)

The benchmark client cancels generation and flushes buffered audio on server
speech detection. Browser calls still pause microphone capture during replies,
so this benchmark is not proof of browser barge-in support. The server currently
does not apply `conversation.item.truncate` to stored assistant context.

### Progressive ASR on the installed Transformers backend

Qwen's installed native streaming API requires the vLLM backend. This server
instead uses bounded rolling-prefix recognition on Transformers: an inference
runs as audio arrives, with only the newest pending prefix retained. Revisable
partial transcripts are emitted as
`conversation.item.input_audio_transcription.partial`, and displayed in call
status. The final hypothesis is reused if it already covers all voiced audio;
otherwise a final decode runs. This begins recognition before the end of the
turn without replacing the inference runtime. It does not reuse an ASR KV cache
and adds compute compared with a single offline decode.

`session.update` accepts `stream_asr` (default true for Qwen ASR, false for
other backends). `realtime_asr_chunk_ms` sets the rolling-prefix interval in
server settings (default 800 ms of received audio). The live path resamples
24 kHz PCM with polyphase filtering, avoiding a first-use librosa/Numba
compilation delay. Short prefixes use a duration-based token limit, bounded
by the configured maximum, to prevent speculative text from blocking later
prefixes. Inference and temporary token observers remain under the model lock.

In the recorded progressive-ASR run, all six input transcripts matched the
canned words. First ASR tokens appeared 0.50–0.73 seconds after speech began;
partial text arrived before speech ended on the four multiword turns. The
single-word interruptions/farewell finished before their first partial.
First audible replies took 1.52–1.97 seconds after user speech ended: this is
still a noticeable, roughly two-second conversational pause. The 700 ms
silence threshold, upstream LLM startup, text buffering, and first audio
generation remain in the critical path. ASR last-token delays ranged from
0.16 to 0.80 seconds after the VAD speech-end boundary.

Interruptions started at 1.003 and 2.001 seconds after audible assistant
onset; simulated playback stopped about 20 and 11 ms after the interrupting
user speech began. These small values reflect a simulated client flushing
its playback buffer on VAD detection, not a physical browser/speaker test.

## Speculative speech and client playback

The realtime call now keeps microphone input active during replies, with browser
echo cancellation. As soon as the latest ASR hypothesis finishes prefilling,
llama starts a provisional response and its text feeds streaming TTS. New input
cancels that candidate and restores the open user turn. Incoming ASR gets priority
over speculative TTS on the shared model lock. The first text chunk is capped at
40 characters at a word boundary; codec output begins with four frames.

Audio streams to the client immediately, including before the user turn is
committed. Every `response.output_audio.delta` has a `generation_id` and binary
`replace_previous`: `true` flushes older queued/playing audio; `false` appends to
the current generation. An empty chunk with `discarded: true` retires a candidate.
The browser holds chunks until its own local speech detector has observed the
configured silence gap and flushes them when speech resumes. Server commitment
does not release playback on its behalf.

The Chat tab's **Call playback delay** slider adjusts that client silence gap
from 0 to 1000 ms in 25 ms steps, including during a call. Preferences survive
reloads in browser storage. Reducing the value immediately checks buffered audio
for playback; changing it does not stop audio already playing. Before a preference
is saved, the client uses the server's silence gap as its default, capped at
1000 ms. This slider
does not change server VAD, ASR, or speculative generation timing.

**Call latency statistics** plots completed prefill batch number against
client-observed delays to the first text chunk, decoded audio, and browser-ready
PCM. All three intervals use the browser monotonic clock, starting at receipt of
the final prefill notification for that prefix; they include notification transport
and event-loop delays and are not exact GPU execution timings. The browser creates
an AudioBuffer on receipt, before the playback gate, so ready-audio statistics
exclude the selected playback delay. Only the first candidate per prefix is counted;
discarded candidates are marked and available samples remain in the averages.
Missing samples are excluded with explicit sample counts. The most recent 200
completed prefixes remain visible after hangup and reset on the next call.

Run `node src/tests/benchmarks/call_stats_test.mjs` for association/average/history
checks. `node src/tests/benchmarks/browser_call.mjs --speech` uses the cached
spoken “Thanks!” fixture and real ASR/LLM/TTS to check the live chart, first playable
audio, slider behavior and reload persistence. `CALL_SPEECH_FIXTURE` can override
the WAV path (mono PCM16 at 24 kHz).

Set the default gap when starting the server:

```sh
S2S_VAD_SILENCE_MS=700 bash src/run.sh
```

The valid range is 0–10000 ms; the default is 700. Session turn-detection settings
can override it. `session.speculative_response=false` disables provisional
responses. This pipeline overlaps computation with the silence gap; it does not
guarantee audio is ready by the end of that gap.

`browser_call.mjs` verifies that the browser holds provisional chunks, drops a
superseded generation, stops playing audio on replacement, and queues continuation
chunks. `speculative_continuation.py` tests resumed speech during a 1000 ms pause
with `S2S_VAD_SILENCE_MS=1400`; use `--output-dir` and `--record-file` to retain its
recording. The synthetic Python client conservatively holds provisional PCM until
server commitment, so its timings differ from the browser's local playback policy.

## ASR token input and concurrent llama prefill

The default LLM upstream is `http://rocbox:18080/v1`, overridable with
`S2S_LLM_URL` in the runner. The patched llama server accepts
`/v1/realtime?input=text&model=...` and returns a server-generated `chat_id`.
The sound server exposes it as `session.llm_chat_id` and `llm.input_session`.
One connection holds the chat's slot/KV across turns; closing it releases the
slot. Reconnection creates a new chat and does not resume an old chat ID.

Individual ASR text-token callbacks now queue transcript updates immediately
for this connection, before final transcription. ASR language/control tokens
are excluded, and text is retokenized with llama's vocabulary. Re-decoding an
already known ASR prefix does not resend duplicate prefixes; new words and
corrections are forwarded as increasing revisions. Incomplete UTF-8 text is
held until it forms a valid character. A final hypothesis also applies any
deletions. On memory backends that cannot roll back a changed suffix, llama
replays known conversation tokens rather than retaining incorrect KV state.

Llama prefills one available `n_batch` per scheduler pass. Incoming updates
are consumed between passes; another pending batch starts immediately without
waiting for a complete word, sentence, or input timer. Output sampling begins
only after `response.create` closes the user turn.

`llm.input_token` records decoded ASR text callback and WebSocket submission
timestamps; `llm.input_prefill` forwards the native batch progress/timestamps.
`session.stream_llm_input` disables/enables this path. Older upstreams without
the capability use chat completions and explicitly report
`llm.input_session.supported=false`, rather than claiming incremental prefill.
Changing upstream settings mid-call also preserves existing local history via
chat completions instead of silently dropping history in a new native chat.

GPU acceptance uses `streaming_input.py` against `rocbox:18080`, a two-slot
Qwen3.5-4B Q8 ROCm server with batch size 64. It checks transcript corrections,
split-word BPE boundaries, no generation before commit, two concurrent isolated
chat IDs, multi-turn history, and an update arriving during an 18-batch prompt.
The new revision was consumed at batch 4; inter-batch scheduler gaps were
27-1036 microseconds in the recorded acceptance run.

The [recorded six-turn integration run](results/book-streaming-input/conversation.wav)
used a single native chat ID across all turns. All six ASR transcripts matched;
17, 4, 7, and 9 lexical prefill batches completed before speech ended on the
four multiword turns. The two short single-word inputs finished before lexical
prefill could begin. Counts exclude empty chat headers. Decoded text callback
to WebSocket submission averaged 0.14 ms (maximum 0.29 ms); this excludes
detokenization, network transit, and llama execution. Audible replies began
1.39-1.72 seconds after speech ended in this run. It used the local server at
the requested endpoint, so differences from earlier remote-upstream runs are
not a controlled attribution of end-to-end speedup to prefill alone.

[Full timings, forwarding traces, and prefill counts](results/book-streaming-input/results.md)
are stored with the recording and raw `results.json`. Run the same benchmark
with `--no-llm-prefill` to compare against post-transcription chat completions.
