"""OpenAI-style Realtime WebSocket bridge for the existing sound-server.

Uses the existing server.py transcribe_impl/synthesize/model registry, plus an
OpenAI-compatible /chat/completions upstream. Import from server.py after the
HTTP routes are defined: `from realtime_extension import install; install(...)`.
"""
import asyncio
import concurrent.futures
import base64
import binascii
import io
import json
import re
import threading
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from voice_profiles import profile_reference, supports_profiles
from server_echo import EchoCanceller


def install(app, server, models, model_settings, transcribe_impl, synthesize, sentences,
            stream_synthesize=None, transcribe_pcm=None):
    def sid(prefix):
        return f'{prefix}_{uuid.uuid4().hex[:20]}'

    def sget(key, default):
        return server.get(key, default)

    llm_base = sget('realtime_llm_base_url', 'http://llama:8080/v1').rstrip('/')
    llm_config_model = sget('realtime_llm_model', '')
    llm_key = sget('realtime_llm_api_key', '')
    audio_max = int(sget('realtime_max_audio_seconds', '120'))
    output_rate = 24000
    max_history = max(2, int(sget('realtime_max_history_messages', '20')))
    vad_gap_ms = int(os.getenv('S2S_VAD_SILENCE_MS', sget('realtime_vad_silence_ms', '700')))
    if not 0 <= vad_gap_ms <= 10000:
        raise ValueError('S2S_VAD_SILENCE_MS must be between 0 and 10000')

    def resolve_llm():
        if llm_config_model:
            return llm_config_model
        headers = {'Authorization': 'Bearer ' + llm_key} if llm_key else {}
        req = urllib.request.Request(llm_base + '/models', headers=headers)
        with urllib.request.urlopen(req, timeout=15) as result:
            listing = json.load(result)
        data = listing.get('data', [])
        if not data:
            raise RuntimeError('No LLM models found; set realtime_llm_model')
        return data[0]['id']

    async def llm_tokens(messages, instructions, max_tokens, stop, selected_model="", on_first_token=None, cache_prompt=True):
        """Bridge blocking upstream SSE onto an async queue without blocking WS receive."""
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        local_stop = threading.Event()

        def push(kind, value=None):
            if not stop.is_set() and not local_stop.is_set():
                loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

        def worker():
            try:
                headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
                if llm_key:
                    headers['Authorization'] = 'Bearer ' + llm_key
                model = selected_model or resolve_llm()
                body = {
                    'model': model,
                    'messages': [{'role': 'system', 'content': instructions}] + messages[-max_history:],
                    'stream': True,
                    'max_tokens': max_tokens,
                    'chat_template_kwargs': {'enable_thinking': False},
                    'cache_prompt': cache_prompt,
                }
                request = urllib.request.Request(
                    llm_base + '/chat/completions',
                    data=json.dumps(body).encode(), headers=headers,
                )
                first_token = True
                with urllib.request.urlopen(request, timeout=int(sget('realtime_llm_timeout', '180'))) as resp:
                    for line in resp:
                        if stop.is_set() or local_stop.is_set():
                            return
                        line = line.decode('utf-8').strip()
                        if not line.startswith('data:'):
                            continue
                        chunk = line[5:].strip()
                        if chunk == '[DONE]':
                            break
                        if not chunk:
                            continue
                        payload = json.loads(chunk)
                        if payload.get('error'):
                            raise RuntimeError(str(payload['error']))
                        for choice in payload.get('choices', []):
                            delta = choice.get('delta') or {}
                            content = delta.get('content') or ''
                            if first_token and (content or delta.get('reasoning_content') or delta.get('reasoning')):
                                push('first_token', time.perf_counter())
                                first_token = False
                            if isinstance(content, list):
                                content = ''.join(x.get('text', '') for x in content if isinstance(x, dict))
                            if content:
                                push('token', content)
            except Exception as exc:
                push('error', str(exc))
            finally:
                push('done')

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                kind, value = await queue.get()
                if kind == 'done':
                    break
                if kind == 'error':
                    raise RuntimeError(value)
                if kind == 'first_token':
                    if on_first_token is not None:
                        on_first_token(value)
                    continue
                yield value
        finally:
            local_stop.set()
            # Worker exits on the next upstream SSE line or HTTP timeout.
            # Do not block response cancellation waiting for a network read.
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass

    def pop_sentence(pending, limit, final=False):
        """Return (ready_sentence, remainder), flushing early on punctuation/length."""
        if not pending.strip():
            return None, '' if final else pending
        # Prefer strong sentence boundaries, retain whitespace after punctuation.
        match = re.search(r'(?<=[.!?。！？])\s+', pending)
        if match:
            return pending[:match.end()].strip(), pending[match.end():]
        if len(pending) >= limit:
            boundary = pending.rfind(' ', 0, limit + 1)
            if boundary <= 0:
                boundary = limit
            return pending[:boundary].strip(), pending[boundary:].lstrip()
        if final:
            return pending.strip(), ''
        return None, pending

    def stt(pcm, rate, model, on_token=None):
        # transcribe_impl needs an UploadFile-compatible object with .file
        class Upload:
            def __init__(self, handle):
                self.file = handle
        arr = np.frombuffer(pcm, dtype='<i2')
        with io.BytesIO() as mem:
            with wave.open(mem, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(arr.tobytes())
            data = mem.getvalue()
        options = {'token_callback': on_token} if models.get(model, {}).get('backend') == 'qwen-asr' else {}
        result = transcribe_impl(model, Upload(io.BytesIO(data)), '', '', False, 'json', **options)
        return result['text']

    def convert_pcm(samples, src_rate):
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if src_rate != output_rate and len(samples):
            length = max(1, round(len(samples) * output_rate / src_rate))
            samples = np.interp(np.arange(length) * (src_rate / output_rate),
                                np.arange(len(samples)), samples).astype(np.float32)
        return (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()

    @app.websocket('/v1/realtime')
    async def realtime(ws: WebSocket):
        requested = ws.query_params.get('model', '')
        tts_mid = requested if requested in models and models[requested]['type'] == 'tts' else sget('default_tts', '')
        stt_mid = sget('default_stt', '') or next((key for key, item in models.items() if item.get('type') == 'stt'), '')
        await ws.accept()
        send_lock = asyncio.Lock()
        state = {
            'id': sid('sess'), 'model': requested or tts_mid, 'object': 'realtime.session',
            'modalities': ['text', 'audio'], 'instructions': sget('realtime_instructions', 'Be helpful, conversational and concise. Respond in natural spoken language.'),
            'voice': sget('realtime_voice', 'Ryan'), 'input_audio_format': 'pcm16',
            'stream_tts': True,
            'stream_asr': transcribe_pcm is not None and models.get(stt_mid, {}).get('backend') == 'qwen-asr',
            'stream_llm_input': True,
            'llm_chat_id': None,
            'llm_cache_prompt': True,
            'speculative_response': True,
            'echo_cancellation': {'available': EchoCanceller.available(), 'enabled': False,
                                  'backend': 'speexdsp', 'frame_ms': 10, 'tail_ms': 300},
            'output_audio_format': 'pcm16', 'input_audio_transcription': {'model': stt_mid},
            'turn_detection': {'type': 'server_vad', 'threshold': 0.025, 'silence_duration_ms': vad_gap_ms, 'prefix_padding_ms': 300,
                               'create_response': True}, 'temperature': 0.8,
            'max_response_output_tokens': int(sget('realtime_max_tokens', '512')),
        }
        history = []
        buffer = bytearray()
        rate = output_rate
        in_speech = False
        silence_ms = 0
        speech_ms = 0
        work = None
        generation = 0
        last_commit = 0
        last_voice_at = 0
        speech_started_at = 0
        live_asr = None
        live_item_id = None
        last_voice_size = 0
        response_timings = {}
        llm_chat = None
        draft = None
        spec_lock = asyncio.Lock()
        spec_tasks = set()
        asr_busy = 0
        asr_ready = asyncio.Event()
        asr_ready.set()
        echo_filter = None

        async def emit(kind, **values):
            async with send_lock:
                await ws.send_json({'event_id': sid('event'), 'type': kind, **values})

        async def error(message, code='invalid_request_error', event_id=None):
            await emit('error', error={'type': 'invalid_request_error', 'code': code, 'message': str(message),
                                       'param': None, 'event_id': event_id})

        async def get_llm_chat():
            nonlocal llm_chat
            if not state.get('stream_llm_input'):
                return None
            model = state.get('llm_model') or llm_config_model
            signature = (model, state['instructions'])
            if llm_chat is not None and llm_chat.signature == signature:
                return llm_chat
            if llm_chat is not None:
                await llm_chat.close()
                llm_chat = None
            if history:
                # Keep established history when switching upstream settings.
                state['stream_llm_input'] = False
                state['llm_input_unavailable'] = 'Upstream settings changed; preserving history through chat completions'
                return None
            from streaming_llm import StreamingChat
            async def forward(event):
                kind = event.pop('type')
                await emit(kind, **event)
            candidate = StreamingChat(llm_base, model, llm_key, state['instructions'],
                                      int(state['max_response_output_tokens']), forward)
            try:
                llm_chat = await candidate.connect()
                llm_chat.before_update = discard_draft
                llm_chat.on_prefill = schedule_draft
                state['llm_chat_id'] = llm_chat.chat_id
                await emit('llm.input_session', chat_id=llm_chat.chat_id, supported=True)
                return llm_chat
            except Exception as exc:
                state['stream_llm_input'] = False
                state['llm_input_unavailable'] = str(exc)
                log_message = f'Incremental LLM input unavailable; using chat completions: {exc}'
                print('[REALTIME]', log_message, flush=True)
                await emit('llm.input_session', supported=False, message=str(exc))
                return None

        async def respond(gen, extra=None, timing=None, preview=None):
            nonlocal work
            rid = sid('resp')
            oid = sid('item')
            send = preview.send if preview is not None else emit
            if preview is not None:
                preview.response_id = rid
            timing = timing or {'origin': time.perf_counter(), 'stages': {}}
            stages = timing['stages']

            def mark(name):
                if name not in stages:
                    stages[name] = round((time.perf_counter() - timing['origin']) * 1000, 2)

            async def report_timing():
                # Cancellation closes the candidate before unwinding its task.
                # Keep stage measurements available for interrupted replies.
                await emit('response.timing', response_id=rid, stages=dict(stages), llm_chat_id=state.get('llm_chat_id'))

            mark('response_started')
            response_timings[rid] = timing
            while len(response_timings) > 20:
                response_timings.pop(next(iter(response_timings)))
            response = {'id': rid, 'object': 'realtime.response', 'status': 'in_progress',
                        'output': [], 'modalities': ['audio', 'text']}
            await send('response.created', response=response,
                       input_revision=preview.revision if preview else None,
                       llm_chat_id=state.get('llm_chat_id'))
            stop = threading.Event()
            sentence_queue = asyncio.Queue(maxsize=8)
            producer = None
            consumer = None
            full_text = []
            try:
                mid = tts_mid
                m = model_settings(mid, 'tts')
                voice = state['voice']
                if voice.lower() in ('alloy', 'ash', 'ballad', 'coral', 'echo', 'fable',
                                     'onyx', 'nova', 'sage', 'shimmer', 'verse', 'marin', 'cedar'):
                    voice = m.get('speaker', m.get('sid', '0'))
                item = {'id': oid, 'type': 'message', 'status': 'in_progress', 'role': 'assistant',
                        'content': [{'type': 'output_audio', 'transcript': ''}]}
                await emit('response.output_item.added', response_id=rid, output_index=0, item=item)
                await emit('response.content_part.added', response_id=rid, item_id=oid, output_index=0,
                           content_index=0, part={'type': 'output_audio', 'transcript': ''})

                async def produce():
                    pending = ''
                    chunk_chars = max(40, int(sget('tts_chunk_chars', '140')))
                    def first_token(at):
                        stages.setdefault('llm_first_token', round((at - timing['origin']) * 1000, 2))
                    async def source():
                        chat = await get_llm_chat()
                        if chat is not None:
                            text = preview.text if preview is not None else (history[-1]['content'] if history and history[-1]['role'] == 'user' else '')
                            async for token in chat.tokens(text, speculative=preview is not None,
                                                          on_started=preview.native_started.set if preview else None):
                                yield token
                        else:
                            async for token in llm_tokens(list(history), state['instructions'],
                                    int(state.get('max_response_output_tokens') or 512), stop,
                                    state.get('llm_model', ''), first_token, state.get('llm_cache_prompt', True)):
                                yield token
                    try:
                        async for token in source():
                            if gen != generation:
                                return
                            full_text.append(token)
                            mark('llm_first_token')
                            mark('llm_first_text_token')
                            print('[REALTIME LLM]', repr(token[:150]), flush=True)
                            pending += token
                            while True:
                                limit = min(chunk_chars, 40) if 'first_text_chunk' not in stages else chunk_chars
                                piece, pending = pop_sentence(pending, limit)
                                if not piece:
                                    break
                                mark('first_text_chunk')
                                await send('response.chunk_ready', response_id=rid)
                                await sentence_queue.put(piece)
                        while pending.strip():
                            piece, pending = pop_sentence(pending, chunk_chars, final=True)
                            if piece:
                                mark('first_text_chunk')
                                await send('response.chunk_ready', response_id=rid)
                                await sentence_queue.put(piece)
                    finally:
                        await sentence_queue.put(None)

                async def consume():
                    while True:
                        piece = await sentence_queue.get()
                        if piece is None:
                            break
                        if gen != generation:
                            return
                        # Explicit language improves CustomVoice fidelity for English speech.
                        # Other languages retain the model's configured language/Auto.
                        language = sget('realtime_tts_language', '').strip()
                        if not language:
                            language = m.get('language', 'Auto')
                        if language.lower() == 'auto' and re.search(r'[A-Za-z]', piece) and not re.search(r'[^\x00-\x7f]', piece):
                            language = 'English'
                        print(f'[REALTIME TTS] model={mid} language={language} text={piece!r}', flush=True)
                        reference = {}
                        if voice.startswith('profile:'):
                            if not supports_profiles(m):
                                raise ValueError('Selected TTS model does not support voice profiles')
                            reference = profile_reference(voice[len('profile:'):])
                        mark('tts_started')
                        async for audio, sr in audio_chunks(mid, m, piece, voice, language, reference):
                            if 'tts_first_audio' not in stages:
                                await send('response.audio_decoded', response_id=rid)
                            mark('tts_first_audio')
                            samples = np.asarray(audio, dtype=np.float32)
                            if samples.size and float(np.sqrt(np.mean(samples * samples))) >= .003:
                                mark('tts_first_speech')
                            raw = convert_pcm(audio, sr)
                            for start in range(0, len(raw), 24000):
                                if gen != generation:
                                    return
                                payload = {'response_id': rid, 'item_id': oid, 'output_index': 0,
                                           'content_index': 0,
                                           'delta': base64.b64encode(raw[start:start + 24000]).decode('ascii')}
                                await send('response.output_audio.delta', **payload)
                                mark('first_audio_sent')
                        await send('response.output_audio_transcript.delta', response_id=rid, item_id=oid,
                                   output_index=0, content_index=0, delta=piece + ' ')

                async def audio_chunks(mid, model, text, voice, language, reference):
                    # A bounded bridge keeps inference off the event loop and
                    # stops the producer when the client cancels or disconnects.
                    await asr_ready.wait()
                    if stop.is_set():
                        return
                    queue = asyncio.Queue(maxsize=4)
                    loop = asyncio.get_running_loop()
                    closed = threading.Event()

                    def put(kind, value):
                        if stop.is_set() or closed.is_set():
                            from qwen_stream import GenerationCancelled
                            raise GenerationCancelled()
                        future = asyncio.run_coroutine_threadsafe(queue.put((kind, value)), loop)
                        while True:
                            try:
                                future.result(timeout=0.1)
                                return
                            except concurrent.futures.TimeoutError:
                                if stop.is_set() or closed.is_set():
                                    future.cancel()
                                    from qwen_stream import GenerationCancelled
                                    raise GenerationCancelled()

                    def worker():
                        try:
                            if stream_synthesize is not None and state.get('stream_tts', True):
                                stream_synthesize(mid, model, text, voice, 1.0, language,
                                    on_audio=lambda audio, rate: put('audio', (audio, rate)),
                                    cancelled=stop, **reference)
                            else:
                                put('audio', synthesize(mid, model, text, voice, 1.0, language, **reference))
                        except Exception as exc:
                            if not stop.is_set() and not closed.is_set():
                                put('error', exc)
                        finally:
                            if not stop.is_set() and not closed.is_set():
                                put('done', None)

                    task = asyncio.create_task(asyncio.to_thread(worker))
                    try:
                        while True:
                            kind, value = await queue.get()
                            if kind == 'done':
                                break
                            if kind == 'error':
                                raise value
                            yield value
                        await task
                    finally:
                        closed.set()

                producer = asyncio.create_task(produce())
                consumer = asyncio.create_task(consume())
                # If either fails, cancel the other rather than waiting indefinitely.
                tasks = {producer, consumer}
                done, pending_tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                failure = next((t.exception() for t in done if not t.cancelled() and t.exception()), None)
                if failure:
                    raise failure
                await asyncio.gather(*pending_tasks)
                if gen != generation:
                    raise asyncio.CancelledError()
                text = ''.join(full_text).strip()
                if not text:
                    raise RuntimeError('LLM returned an empty response')
                if preview is not None:
                    await preview.committed.wait()
                    if preview.closed:
                        raise asyncio.CancelledError()
                history.append({'role': 'assistant', 'content': text})
                del history[:-max_history]
                await send('response.output_audio.done', response_id=rid, item_id=oid, output_index=0, content_index=0)
                await send('response.output_audio_transcript.done', response_id=rid, item_id=oid, output_index=0,
                           content_index=0, transcript=text)
                await send('response.content_part.done', response_id=rid, item_id=oid, output_index=0,
                           content_index=0, part={'type': 'output_audio', 'transcript': text})
                item['status'] = 'completed'
                item['content'][0]['transcript'] = text
                await send('response.output_item.done', response_id=rid, output_index=0, item=item)
                response.update(status='completed', output=[item])
                mark('response_done')
                await report_timing()
                print(f'[REALTIME] response {rid} completed; text_chars={len(text)}', flush=True)
                await send('response.done', response=response)
            except asyncio.CancelledError:
                response['status'] = 'cancelled'
                mark('response_cancelled')
                print(f'[REALTIME] response {rid} cancelled', flush=True)
                try:
                    await report_timing()
                    await send('response.output_audio.done', response_id=rid, item_id=oid, output_index=0, content_index=0)
                    await send('response.done', response=response)
                except Exception:
                    pass
                raise
            except Exception as exc:
                if preview is not None:
                    preview.failure = str(exc)
                    preview.native_started.set()
                import traceback
                traceback.print_exc()
                await send('error', error={'type': 'server_error', 'message': f'Realtime response failed: {exc}'})
                mark('response_failed')
                await report_timing()
                response['status'] = 'failed'
                response['status_details'] = {'type': 'server_error', 'error': {'message': str(exc)}}
                await send('response.done', response=response)
            finally:
                stop.set()
                for task in (producer, consumer):
                    if task and not task.done():
                        task.cancel()
                # Do not wait on blocking model inference during cancellation.

        async def discard_draft(allow_accepted=False):
            nonlocal draft, work, generation
            async with spec_lock:
                current = draft
                if current is None or (current.accepted and not allow_accepted):
                    return
                draft = None
                current.closed = True
                current.committed.set()
                generation += 1
                if current.task is not None:
                    current.task.cancel()
                    await asyncio.gather(current.task, return_exceptions=True)
                if work is current.task:
                    work = None
                if llm_chat is not None and llm_chat.preview:
                    await llm_chat.discard()
                await emit('response.output_audio.delta', delta='', replace_previous=True,
                           generation_id=current.id, provisional=True, discarded=True)
                await emit('response.discarded', response_id=current.response_id,
                           generation_id=current.id, provisional=True)

        async def start_draft(text, revision):
            nonlocal draft, work, generation
            async with spec_lock:
                if (draft is not None or (work is not None and not work.done()) or llm_chat is None
                        or not llm_chat.supports_preview or not state.get('speculative_response')
                        or revision != llm_chat.revision or text != llm_chat.last_text):
                    return
                from speculative_audio import Candidate
                generation += 1
                draft = Candidate(text, revision, emit)
                work = asyncio.create_task(respond(generation, timing=draft.timing, preview=draft))
                draft.task = work

        async def schedule_draft(text, revision):
            task = asyncio.create_task(start_draft(text, revision))
            spec_tasks.add(task)
            def finished(done):
                spec_tasks.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)

        async def before_asr():
            nonlocal asr_busy
            asr_busy += 1
            asr_ready.clear()
            await discard_draft()

        async def after_asr():
            nonlocal asr_busy
            asr_busy = max(0, asr_busy - 1)
            if not asr_busy:
                asr_ready.set()

        async def commit(auto=False, create_response=False):
            nonlocal in_speech, silence_ms, speech_ms, buffer, work, generation, last_commit, last_voice_at, speech_started_at, live_asr, live_item_id, last_voice_size
            if not buffer:
                if not auto:
                    await error('Input audio buffer is empty', 'empty_audio_buffer')
                return
            audio = bytes(buffer)
            buffer.clear()
            in_speech = False
            silence_ms = 0
            speech_ms = 0
            last_commit = time.monotonic()
            item_id = live_item_id or sid('item')
            live_item_id = None
            recognizer, live_asr = live_asr, None
            voiced_size, last_voice_size = last_voice_size, 0
            now = time.perf_counter()
            origin = last_voice_at if auto and last_voice_at else now
            last_voice_at = 0
            committed = round((now - origin) * 1000, 2)
            timing = {'origin': origin, 'stages': {
                'speech_end': 0.0, 'turn_committed': committed, 'asr_started': committed}}
            if speech_started_at:
                timing['stages']['speech_started'] = round((speech_started_at - origin) * 1000, 2)
            speech_started_at = 0

            def asr_token(kind):
                elapsed = round((time.perf_counter() - origin) * 1000, 2)
                if kind == 'start':
                    timing['stages'].setdefault('asr_generation_started', elapsed)
                elif kind == 'token':
                    timing['stages'].setdefault('asr_first_token', elapsed)
                    timing['stages']['asr_last_token'] = elapsed
                elif kind == 'end':
                    timing['stages']['asr_tokens_done'] = elapsed
            await emit('input_audio_buffer.committed', previous_item_id=None, item_id=item_id)
            await emit('conversation.item.created', previous_item_id=None,
                       item={'id': item_id, 'type': 'message', 'role': 'user', 'status': 'completed',
                             'content': [{'type': 'input_audio', 'transcript': None}]})
            try:
                if recognizer is not None:
                    transcript = (await recognizer.finish(audio, voiced_size)).strip()
                    for stage, at in recognizer.raw_timings.items():
                        timing['stages'][stage] = round((at - origin) * 1000, 2)
                else:
                    transcript = (await asyncio.to_thread(stt, audio, rate, stt_mid, asr_token)).strip()
                timing['stages']['asr_completed'] = round((time.perf_counter() - timing['origin']) * 1000, 2)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                await emit('conversation.item.input_audio_transcription.failed', item_id=item_id, content_index=0,
                           error={'type': 'transcription_error', 'message': str(exc)})
                return
            await emit('conversation.item.input_audio_transcription.completed', item_id=item_id,
                       content_index=0, transcript=transcript)
            print('[REALTIME STT]', repr(transcript), 'auto=', auto, 'create_response=', create_response, flush=True)
            if transcript:
                history.append({'role': 'user', 'content': transcript})
                del history[:-max_history]
                if create_response:
                    chat = await get_llm_chat()
                    if chat is not None and chat.supports_preview and state.get('speculative_response'):
                        await chat.update(transcript, final=True)
                        await chat.queue.join()
                        await start_draft(transcript, chat.revision)
                        current = draft
                        if current is not None and not current.closed and current.text == transcript:
                            await current.commit(chat, timing)
                            return
                    if work is not None and not work.done():
                        return
                    generation += 1
                    if work and not work.done():
                        work.cancel()
                    print('[REALTIME] launching response after transcription', flush=True)
                    work = asyncio.create_task(respond(generation, timing=timing))

        await emit('session.created', session=state)
        try:
            while True:
                event = await ws.receive_json()
                typ = event.get('type')
                try:
                    if typ == 'session.update':
                        update = event.get('session') or {}
                        if 'instructions' in update:
                            state['instructions'] = str(update['instructions'])
                        if 'llm_model' in update:
                            state['llm_model'] = str(update['llm_model'])
                        if 'stream_tts' in update:
                            state['stream_tts'] = bool(update['stream_tts'])
                        if 'stream_asr' in update:
                            if live_asr is not None:
                                await live_asr.close()
                                live_asr = None
                            state['stream_asr'] = bool(update['stream_asr']) and transcribe_pcm is not None and models.get(stt_mid, {}).get('backend') == 'qwen-asr'
                        if 'stream_llm_input' in update:
                            state['stream_llm_input'] = bool(update['stream_llm_input'])
                            if not state['stream_llm_input'] and llm_chat is not None:
                                await llm_chat.close()
                                llm_chat = None
                        if 'llm_cache_prompt' in update:
                            state['llm_cache_prompt'] = bool(update['llm_cache_prompt'])
                        if 'speculative_response' in update:
                            if not update['speculative_response']:
                                await discard_draft()
                            state['speculative_response'] = bool(update['speculative_response'])
                        if 'voice' in update:
                            state['voice'] = str(update['voice'])
                        if 'max_response_output_tokens' in update:
                            state['max_response_output_tokens'] = update['max_response_output_tokens']
                        if 'turn_detection' in update:
                            value = update['turn_detection']
                            state['turn_detection'] = {**(state.get('turn_detection') or {}), **value} if isinstance(value, dict) else value
                        for key in ('input_audio_format', 'output_audio_format', 'input_audio_transcription'):
                            if key in update:
                                state[key] = update[key]
                        audio_settings = update.get('audio') or {}
                        if isinstance(audio_settings, dict):
                            input_settings = audio_settings.get('input') or {}
                            output_settings = audio_settings.get('output') or {}
                            fmt = input_settings.get('format')
                            if isinstance(fmt, dict) and fmt.get('type') == 'audio/pcm':
                                rate = int(fmt.get('rate', 24000))
                            if 'voice' in output_settings:
                                state['voice'] = str(output_settings['voice'])
                            if 'turn_detection' in input_settings:
                                value = input_settings['turn_detection']
                                state['turn_detection'] = {**(state.get('turn_detection') or {}), **value} if isinstance(value, dict) else value
                        if state['input_audio_format'] != 'pcm16' or state['output_audio_format'] != 'pcm16':
                            await error('Only PCM16 input/output supported', 'unsupported_audio_format')
                        else:
                            echo = update.get('echo_cancellation')
                            requested_echo = bool(echo.get('enabled')) if isinstance(echo, dict) else state['echo_cancellation']['enabled']
                            if echo_filter is not None and (not requested_echo or echo_filter.sample_rate != rate):
                                echo_filter.close()
                                echo_filter = None
                            if requested_echo and state['echo_cancellation']['available'] and echo_filter is None:
                                echo_filter = EchoCanceller(sample_rate=rate)
                            state['echo_cancellation']['enabled'] = echo_filter is not None
                            await get_llm_chat()
                            await emit('session.updated', session=state)
                    elif typ == 'input_audio_buffer.append':
                        data = base64.b64decode(event.get('audio', ''), validate=True)
                        if len(data) & 1:
                            raise ValueError('PCM16 input requires even byte count')
                        if echo_filter is not None:
                            reference = event.get('playback_reference')
                            reference = base64.b64decode(reference, validate=True) if reference is not None else None
                            data = echo_filter.process(data, reference)
                            if data:
                                clean = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768
                                rms = float(np.sqrt(np.mean(clean * clean)))
                                detection = state.get('turn_detection') or {}
                                await emit('input_audio_buffer.activity',
                                    voiced=rms >= float(detection.get('threshold', .025)),
                                    rms=rms, captured_at=event.get('captured_at'))
                        if len(buffer) + len(data) > audio_max * rate * 2:
                            buffer.clear()
                            raise ValueError('Audio input exceeds configured duration limit')
                        buffer.extend(data)
                        vad = state.get('turn_detection')
                        if isinstance(vad, dict) and vad.get('type') == 'server_vad' and data:
                            threshold = float(vad.get('threshold', 0.025))
                            samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768
                            rms = float(np.sqrt(np.mean(samples * samples)))
                            duration = round(1000 * len(samples) / rate)
                            if rms >= threshold:
                                if silence_ms > 0 and draft is not None and not draft.accepted:
                                    await discard_draft()
                                last_voice_at = time.perf_counter()
                                last_voice_size = len(buffer)
                                silence_ms = 0
                                speech_ms += duration
                                if not in_speech:
                                    if draft is not None and draft.accepted:
                                        await discard_draft(allow_accepted=True)
                                    in_speech = True
                                    speech_started_at = time.perf_counter()
                                    live_item_id = sid('item')
                                    await emit('input_audio_buffer.speech_started', audio_start_ms=0, item_id=live_item_id)
                                    chat = await get_llm_chat()
                                    if chat is not None:
                                        await chat.begin_turn()
                                    if state.get('stream_asr'):
                                        from live_asr import LiveASR
                                        async def partial(text, item=live_item_id):
                                            if chat is not None:
                                                await chat.update(text, final=True)
                                                await chat.queue.join()
                                                await schedule_draft(chat.last_text, chat.revision)
                                            await emit('conversation.item.input_audio_transcription.partial',
                                                item_id=item, content_index=0, transcript=text)
                                        live_asr = LiveASR(
                                            lambda pcm, callback, upstream=chat: transcribe_pcm(
                                                stt_mid, pcm, rate, callback,
                                                text_callback=upstream.update_from_thread if upstream else None),
                                            partial, rate=rate,
                                            interval_ms=int(sget('realtime_asr_chunk_ms', '800')),
                                            before_recognize=before_asr, after_recognize=after_asr)
                            elif in_speech:
                                if silence_ms == 0 and draft is None and llm_chat is not None and llm_chat.last_text:
                                    if llm_chat.prefilled_revision == llm_chat.revision:
                                        await schedule_draft(llm_chat.last_text, llm_chat.revision)
                                silence_ms += duration
                                if silence_ms >= int(vad.get('silence_duration_ms', vad_gap_ms)) and speech_ms >= 200:
                                    await emit('input_audio_buffer.speech_stopped', audio_end_ms=round(len(buffer) / 2 / rate * 1000),
                                               item_id=live_item_id or sid('item'))
                                    await commit(auto=True, create_response=True)
                            else:
                                # Keep only pre-roll while listening, so idle calls
                                # do not fill the maximum utterance buffer.
                                keep = max(1, int(vad.get('prefix_padding_ms', 300)) * rate // 1000) * 2
                                if len(buffer) > keep:
                                    del buffer[:-keep]
                            if in_speech and live_asr is not None:
                                live_asr.submit(buffer)
                    elif typ == 'input_audio_buffer.commit':
                        await commit(create_response=True)
                    elif typ == 'input_audio_buffer.clear':
                        await discard_draft()
                        buffer.clear()
                        in_speech = False
                        silence_ms = speech_ms = 0
                        speech_started_at = last_voice_at = 0
                        last_voice_size = 0
                        live_item_id = None
                        if live_asr is not None:
                            await live_asr.close()
                            live_asr = None
                        await emit('input_audio_buffer.cleared')
                    elif typ == 'response.create':
                        print('[REALTIME] client response.create; active=', work is not None and not work.done(), flush=True)
                        # Open WebUI may request a second response after server_vad
                        # already started one. Do not cancel the first response.
                        if work is not None and not work.done():
                            continue
                        generation += 1
                        if work and not work.done():
                            work.cancel()
                        print('[REALTIME] launching client-requested response', flush=True)
                        work = asyncio.create_task(respond(generation, event.get('response')))
                    elif typ == 'response.playback_started':
                        timing = response_timings.get(event.get('response_id'))
                        if timing is not None:
                            stages = timing['stages']
                            stages.setdefault('playback_ack_received', round((time.perf_counter() - timing['origin']) * 1000, 2))
                            playback = event.get('receive_to_playback_ms')
                            if isinstance(playback, (float, int)) and 0 <= playback < 60000:
                                stages.setdefault('browser_receive_to_playback', round(playback, 2))
                            await emit('response.timing', response_id=event.get('response_id'), stages=dict(stages))
                    elif typ == 'response.cancel':
                        if draft is not None:
                            await discard_draft(allow_accepted=True)
                        generation += 1
                        if work and not work.done():
                            work.cancel()
                    elif typ == 'conversation.item.create':
                        item = event.get('item', {})
                        content = item.get('content', [])
                        if item.get('type') == 'function_call_output':
                            # A tool result is not a valid standalone Chat Completions tool message.
                            history.append({'role': 'user', 'content': 'Tool result: ' + str(item.get('output', ''))})
                        elif item.get('role') in ('user', 'assistant'):
                            text = ' '.join(str(c.get('text', '')) for c in content if c.get('type') in ('input_text', 'text'))
                            if text.strip():
                                history.append({'role': item['role'], 'content': text})
                        await emit('conversation.item.created', previous_item_id=None, item=item)
                    elif typ in ('conversation.item.truncate', 'conversation.item.delete'):
                        # No-op: generated history stores text, not sample-accurate playback positions.
                        pass
                    else:
                        await error(f'Unsupported realtime event: {typ}', 'unsupported_event', event.get('event_id'))
                except (ValueError, TypeError, binascii.Error, KeyError) as exc:
                    await error(exc, 'invalid_request_error', event.get('event_id'))
        except WebSocketDisconnect:
            pass
        finally:
            if echo_filter is not None:
                echo_filter.close()
            generation += 1
            if draft is not None:
                draft.closed = True
                draft.committed.set()
            for task in list(spec_tasks):
                task.cancel()
            await asyncio.gather(*spec_tasks, return_exceptions=True)
            if live_asr is not None:
                await live_asr.close()
            if llm_chat is not None:
                await llm_chat.close()
            if work and not work.done():
                work.cancel()
