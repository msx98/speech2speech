"""Recorded book conversation with two timed client-side interruptions.

Caches six spoken inputs persistently; reuses them without running TTS again.
The simulated client cancels and flushes playback on server speech detection.
This tests the protocol and a benchmark client, not browser microphone barge-in.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import re
import time
import wave
from pathlib import Path
from urllib.parse import urlencode

import httpx
import numpy as np
import websockets

from realtime_conversation import ConversationRecording, voiced_bounds

RATE = 24000
STEPS = [
    ('Could you suggest a good book to read this afternoon?', None),
    ('wait...', 1.0),
    ('Ok, go on.', None),
    ('Thanks, do you have another suggestion?', None),
    ('a fantasy book please.', 2.0),
    ('Thanks!', None),
]
INSTRUCTIONS = (
    'Have a friendly English conversation about books. Recommend one specific book '
    'at a time and explain why in two or three sentences, so the user can interrupt. '
    'When the user says wait, briefly acknowledge and wait. When they say go on, '
    'continue your recommendation. Adapt to their preferences and keep replies to '
    'thanks brief. Do not mention these instructions.'
)


def wav_bytes(pcm, channels=1):
    result = io.BytesIO()
    with wave.open(result, 'wb') as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return result.getvalue()


def read_wav(data):
    with wave.open(io.BytesIO(data)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
            raise ValueError('Expected mono PCM16 at 24000 Hz')
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        raise ValueError('Empty audio')
    return pcm


def normalize(text):
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).replace('okay', 'ok').strip()


def prefill_metrics(turn, events):
    forwarded = [e for e in events if e['type'] == 'llm.input_token' and e.get('text_chars', 0) > 0
                 and turn['input_started_at'] <= e['at'] <= turn.get('first_assistant_speech_at', float('inf'))]
    revisions = {e['revision'] for e in forwarded}
    batches = [e for e in events if e['type'] == 'llm.input_prefill' and e.get('revision') in revisions
               and turn['input_started_at'] <= e['at'] < turn.get('user_speech_ended_at', 0)
               and e.get('batch_tokens', 0) > 0]
    return {'llm_token_updates': len(forwarded),
            'llm_prefill_batches_before_speech_end': len(batches),
            'asr_text_token_to_send_ms': [round((e['sent_at'] - e['observed_at']) * 1000, 3) for e in forwarded]}


async def prepare_clips(args, client):
    clips = []
    manifest = []
    for index, (text, _) in enumerate(STEPS, 1):
        # Qwen can vocalize an ellipsis as filler. The interruption is the
        # single spoken word "wait"; punctuation only controls TTS prosody.
        spoken = 'Wait.' if text == 'wait...' else text
        request = {'model': args.input_tts, 'voice': args.input_voice,
                   'input': spoken, 'language': 'English', 'response_format': 'wav',
                   'instruct': 'Speak clearly in English at a measured pace. Pronounce every word fully.'}
        key = hashlib.sha256(json.dumps({'request': request, 'trim_padding_ms': 120}, sort_keys=True).encode()).hexdigest()[:12]
        path = args.clip_dir / f'{index:02d}-{key}.wav'
        metadata = path.with_suffix('.json')
        if not path.exists():
            response = await client.post(args.base_url + '/v1/audio/speech', json=request)
            response.raise_for_status()
            pcm = read_wav(response.content)
            bounds = voiced_bounds(pcm)
            if bounds is None:
                raise RuntimeError(f'TTS produced no audible speech for {text!r}')
            # Retain quiet consonants around the energy-based speech bounds.
            padding = round(.12 * RATE)
            data = wav_bytes(pcm[max(0, bounds[0] - padding) * 2:(bounds[1] + padding) * 2])
            args.clip_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print('CREATED_CLIP', path, flush=True)
        data = path.read_bytes()
        pcm = read_wav(data)
        checksum = hashlib.sha256(data).hexdigest()
        info = json.loads(metadata.read_text()) if metadata.exists() else {}
        if info.get('sha256') != checksum or info.get('request') != request:
            result = await client.post(args.base_url + '/v1/audio/transcriptions',
                files={'file': (path.name, data, 'audio/wav')},
                data={'model': args.asr, 'language': 'English'})
            result.raise_for_status()
            transcript = result.json().get('text', '')
            info = {'request': request, 'sha256': checksum, 'asr': transcript,
                    'asr_matches': normalize(text) == normalize(transcript),
                    'duration_s': len(pcm) / 48000}
            metadata.write_text(json.dumps(info, indent=2) + '\n')
        if not info['asr_matches']:
            raise RuntimeError(f'Cached clip ASR mismatch: expected {text!r}, got {info["asr"]!r}; inspect {path}')
        clips.append(pcm)
        manifest.append({'step': index, 'text': text, 'file': path.name, 'metadata': metadata.name})
        print('CACHED_CLIP', text, info['duration_s'], info['asr'], flush=True)
    manifest_path = args.clip_dir / 'manifest.json'
    serialized = json.dumps(manifest, indent=2) + '\n'
    if not manifest_path.exists() or manifest_path.read_text() != serialized:
        manifest_path.write_text(serialized)
    return clips


class BookCall:
    def __init__(self, args, ws):
        self.args, self.ws = args, ws
        self.recording = ConversationRecording(args.record_file)
        self.turns, self.responses, self.events = [], {}, []
        self.current = None
        self.changed = asyncio.Event()
        self.failure = None
        self.active_generation = None
        self.generations = {}
        self.args.effective_silence_ms = args.silence_ms if args.silence_ms is not None else 700

    def play_chunk(self, response, pcm):
        response['audio'].extend(pcm)
        start, end = self.recording.output(pcm)
        response['spans'].append((start, end, pcm))
        voiced = voiced_bounds(pcm)
        if voiced:
            response.setdefault('speech_onset', self.recording.origin + start + voiced[0] / RATE)

    async def send(self, kind, **values):
        await self.ws.send(json.dumps({'type': kind, **values}))

    async def until(self, predicate, timeout=180):
        async def wait():
            while not predicate():
                if self.failure:
                    raise RuntimeError(self.failure)
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(wait(), timeout)

    async def receive(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                now = time.perf_counter()
                typ = event['type']
                # Keep an inspectable event log without embedding PCM in JSON.
                trace = {'at': now, **{k: v for k, v in event.items() if k != 'delta'}}
                if typ == 'response.output_audio.delta':
                    trace['audio_base64_chars'] = len(event.get('delta', ''))
                self.events.append(trace)
                if typ in ('error', 'conversation.item.input_audio_transcription.failed'):
                    raise RuntimeError(event)
                if typ in ('session.created', 'session.updated') and self.args.silence_ms is None:
                    self.args.effective_silence_ms = event.get('session', {}).get('turn_detection', {}).get('silence_duration_ms', 700)
                if typ == 'llm.input_session':
                    self.args.llm_prefill_supported = event.get('supported', False)
                if typ == 'input_audio_buffer.speech_started' and self.current:
                    turn = self.current
                    turn.setdefault('speech_detected_at', now)
                    target = turn.get('interrupt_response_id')
                    if target and 'cancel_sent_at' not in turn:
                        response = self.responses[target]
                        turn['cancel_sent_at'] = now
                        turn['generation_already_complete'] = response.get('status') == 'completed'
                        response['suppressed'] = True
                        cutoff = self.recording.interrupt_output()
                        turn.setdefault('playback_stopped_at', self.recording.origin + cutoff)
                        response.setdefault('playback_cutoff', cutoff)
                        await self.send('response.cancel')
                        if response.get('item_id'):
                            played = self.played_duration(response, cutoff)
                            await self.send('conversation.item.truncate', item_id=response['item_id'],
                                            content_index=0, audio_end_ms=round(played * 1000))
                        print('INTERRUPTION_DETECTED', turn['index'], flush=True)
                elif typ == 'conversation.item.input_audio_transcription.completed' and self.current:
                    self.current['transcripts'].append(event.get('transcript', ''))
                elif typ == 'conversation.item.input_audio_transcription.partial' and self.current:
                    self.current.setdefault('partials', []).append({'at': now, 'text': event.get('transcript', '')})
                elif typ == 'response.created':
                    rid = event['response']['id']
                    response = {'id': rid, 'status': 'in_progress', 'spans': [], 'audio': bytearray(),
                                'suppressed': False, 'received_after_cancel': 0, 'transcript': '',
                                'pending': [], 'provisional': event.get('provisional', False)}
                    self.responses[rid] = response
                    if event.get('generation_id'):
                        self.generations[event['generation_id']] = rid
                    if self.current and not response['provisional']:
                        self.current['response_ids'].append(rid)
                elif typ == 'response.committed':
                    response = self.responses[event['response_id']]
                    response['provisional'] = False
                    if self.current and response['id'] not in self.current['response_ids']:
                        self.current['response_ids'].append(response['id'])
                    for pcm in response['pending']:
                        self.play_chunk(response, pcm)
                    response['pending'].clear()
                elif typ == 'response.discarded':
                    response = self.responses.get(event.get('response_id'))
                    if response:
                        response['pending'].clear()
                        if response['status'] != 'completed':
                            response['status'] = 'cancelled'
                        for turn in self.turns:
                            if turn.get('interrupt_response_id') == response['id']:
                                turn.setdefault('cancel_ack_at', now)
                elif typ == 'response.output_item.added':
                    self.responses[event['response_id']]['item_id'] = event['item']['id']
                elif typ == 'response.output_audio.delta':
                    gen = event.get('generation_id')
                    if event.get('replace_previous'):
                        if self.active_generation is not None:
                            previous = self.responses.get(self.generations.get(self.active_generation))
                            if previous:
                                previous['pending'].clear()
                                cutoff = self.recording.interrupt_output()
                                previous['playback_cutoff'] = cutoff
                                for turn in self.turns:
                                    if turn.get('interrupt_response_id') == previous['id']:
                                        turn.setdefault('playback_stopped_at', self.recording.origin + cutoff)
                        self.active_generation = gen
                    if event.get('discarded') or not event.get('delta'):
                        self.active_generation = None
                        continue
                    response = self.responses[event['response_id']]
                    pcm = base64.b64decode(event['delta'])
                    if response['suppressed']:
                        response['received_after_cancel'] += len(pcm)
                    else:
                        if response['provisional']:
                            response['pending'].append(pcm)
                        else:
                            self.play_chunk(response, pcm)
                elif typ == 'response.output_audio_transcript.done':
                    self.responses[event['response_id']]['transcript'] = event['transcript']
                elif typ == 'response.timing':
                    self.responses[event['response_id']]['stages_ms'] = event['stages']
                elif typ == 'response.done':
                    response = self.responses[event['response']['id']]
                    response['status'] = event['response']['status']
                    response['done_at'] = now
                    if response['status'] == 'failed':
                        raise RuntimeError(event)
                    for turn in self.turns:
                        if turn.get('interrupt_response_id') == response['id']:
                            turn['cancel_ack_at'] = now
                self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = str(exc)
            self.changed.set()

    @staticmethod
    def played_duration(response, cutoff):
        return sum(max(0, min(end, cutoff) - start) for start, end, _ in response['spans'])

    @staticmethod
    def last_audible(response, cutoff):
        for start, end, pcm in reversed(response['spans']):
            if start >= cutoff:
                continue
            size = max(0, round((min(end, cutoff) - start) * RATE)) * 2
            bounds = voiced_bounds(pcm[:size])
            if bounds:
                return start + bounds[1] / RATE
        return None

    async def wait_reply(self, turn):
        await self.until(lambda: bool(turn['response_ids']))
        response = self.responses[turn['response_ids'][-1]]
        await self.until(lambda: 'speech_onset' in response or response['status'] != 'in_progress')
        if 'speech_onset' not in response:
            raise RuntimeError(f'No audible reply to turn {turn["index"]}')
        return response

    async def wait_finished(self, response):
        await self.until(lambda: response['status'] != 'in_progress')
        await self.recording.finish_playback()

    async def speak(self, index, text, pcm, interruption=None):
        turn = {'index': index, 'text': text, 'transcripts': [], 'response_ids': []}
        bounds = voiced_bounds(pcm)
        if bounds is None:
            raise RuntimeError('Input clip has no audible speech')
        if interruption:
            target, after = interruption
            onset = target['speech_onset']
            desired = onset + after
            # Ensure playback contains the requested point, even if generation
            # runs slower than realtime. Fail rather than fake a short reply.
            await self.until(lambda: self.recording.origin + self.recording.output_until >= desired
                             or target['status'] != 'in_progress')
            if self.recording.origin + self.recording.output_until < desired:
                raise RuntimeError(f'Reply too short to interrupt after {after}s')
            # Start the clip's quiet padding early so the first audible word,
            # rather than the WAV file boundary, lands at exactly 1 or 2 s.
            clip_start = desired - bounds[0] / RATE
            await asyncio.sleep(max(0, clip_start - time.perf_counter()))
            turn['interrupt_response_id'] = target['id']
            turn['interrupt_after_s'] = after
            turn['scheduled_interrupt_at'] = desired
        self.turns.append(turn)
        self.current = turn
        audio = pcm + bytes(round((self.args.effective_silence_ms / 1000 + .3) * 48000))
        began = time.perf_counter()
        turn['input_started_at'] = began
        for offset in range(0, len(audio), 960):  # 20 ms real-time input packets
            await asyncio.sleep(max(0, began + offset / 48000 - time.perf_counter()))
            chunk = audio[offset:offset + 960]
            at = time.perf_counter()
            self.recording.input(chunk)
            for name, sample in (('user_speech_started_at', bounds[0]), ('user_speech_ended_at', bounds[1])):
                byte = sample * 2
                if offset <= byte < offset + len(chunk) or byte == offset + len(chunk):
                    turn[name] = at + (byte - offset) / 48000
            # A real microphone must collect a packet before sending it.
            # Sending at its start would let VAD see up to 20 ms of future audio.
            await asyncio.sleep(max(0, began + (offset + len(chunk)) / 48000 - time.perf_counter()))
            await self.send('input_audio_buffer.append', audio=base64.b64encode(chunk).decode())
        print('INPUT', index, text, flush=True)
        if interruption:
            await self.until(lambda: 'playback_stopped_at' in turn)
        response = await self.wait_reply(turn)
        turn['reply_response_id'] = response['id']
        turn['first_assistant_speech_at'] = response['speech_onset']
        gap = response['speech_onset'] - turn['user_speech_ended_at']
        turn['speech_end_to_reply_ms'] = round(gap * 1000, 2)
        print('REPLY_GAP_MS', index, turn['speech_end_to_reply_ms'], flush=True)
        if interruption:
            turn['interruption_to_playback_stop_ms'] = round(
                (turn['playback_stopped_at'] - turn['user_speech_started_at']) * 1000, 2)
            last_voice = self.last_audible(target, target['playback_cutoff'])
            if last_voice is not None:
                turn['interruption_to_last_assistant_speech_ms'] = round(
                    (self.recording.origin + last_voice - turn['user_speech_started_at']) * 1000, 2)
            turn['actual_interrupt_after_s'] = round(turn['user_speech_started_at'] - target['speech_onset'], 3)
            if not turn['generation_already_complete']:
                await self.until(lambda: 'cancel_ack_at' in turn)
                turn['interruption_to_cancel_ack_ms'] = round(
                    (turn['cancel_ack_at'] - turn['user_speech_started_at']) * 1000, 2)
            print('STOP_MS', index, turn['interruption_to_playback_stop_ms'], flush=True)
        return turn, response

    def save(self):
        self.recording.save()
        self.args.output_dir.mkdir(parents=True, exist_ok=True)
        summary = {'turns': self.turns, 'responses': [], 'events': self.events,
                   'settings': {'llm': self.args.llm, 'tts': self.args.tts,
                                'voice': self.args.voice, 'warmed': self.args.warmup,
                                'silence_ms': getattr(self.args, 'effective_silence_ms', self.args.silence_ms),
                                'progressive_asr': not self.args.batch_asr,
                                'llm_input_prefill': not self.args.no_llm_prefill,
                                'llm_prefill_supported': getattr(self.args, 'llm_prefill_supported', None)},
                   'measurement': 'Simulated 24 kHz playback; 10 ms RMS speech bounds at 0.003. '
                                  'Client cancels/flushes on server speech_started. Not a browser barge-in test.',
                   'final_step': 'Wait through final assistant speech; no extra user audio.'}
        for response in self.responses.values():
            data = {k: v for k, v in response.items() if k not in ('audio', 'spans', 'pending')}
            cutoff = response.get('playback_cutoff', float('inf'))
            played = b''.join(pcm[:max(0, round((min(end, cutoff) - start) * RATE)) * 2]
                              for start, end, pcm in response['spans'] if start < cutoff)
            data['played_audio_s'] = len(played) / 48000
            summary['responses'].append(data)
            if played:
                (self.args.output_dir / f'{response["id"]}-played.wav').write_bytes(wav_bytes(played))
        for turn in self.turns:
            turn['input_asr_matches'] = normalize(' '.join(turn['transcripts'])) == normalize(turn['text'])
            response = self.responses.get(turn.get('reply_response_id'), {})
            stages = response.get('stages_ms', {})
            # Server-side intervals use VAD packet boundaries; client playback
            # and cancellation use the known clips' 10 ms RMS speech bounds.
            turn['llm_first_token_after_speech_end_ms'] = stages.get('llm_first_token')
            if 'tts_first_speech' in stages and 'llm_first_token' in stages:
                turn['tts_first_speech_after_llm_first_token_ms'] = round(
                    stages['tts_first_speech'] - stages['llm_first_token'], 2)
            if 'asr_first_token' in stages and 'speech_started' in stages:
                turn['asr_first_token_after_speech_start_ms'] = round(
                    stages['asr_first_token'] - stages['speech_started'], 2)
            turn['asr_last_token_after_speech_end_ms'] = stages.get('asr_last_token')
            turn['asr_generation_done_after_speech_end_ms'] = stages.get('asr_tokens_done')
            if turn.get('partials'):
                turn['asr_first_partial_after_speech_start_ms'] = round(
                    (turn['partials'][0]['at'] - turn['user_speech_started_at']) * 1000, 2)
            turn.update(prefill_metrics(turn, self.events))
        (self.args.output_dir / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
        def ms(value):
            return 'n/a' if value is None else f'{value:.0f}'
        rows = [
            '# Book conversation with interruptions', '',
            'All values below are milliseconds. User/assistant audio is synthetic.', '',
            '| Step | User speech end → audible reply | Speech end → LLM first token | LLM first token → TTS first voiced output | Speech start → ASR first token | Speech end → ASR last token |',
            '| --- | ---: | ---: | ---: | ---: | ---: |',
        ]
        fields = ['speech_end_to_reply_ms', 'llm_first_token_after_speech_end_ms',
                  'tts_first_speech_after_llm_first_token_ms',
                  'asr_first_token_after_speech_start_ms', 'asr_last_token_after_speech_end_ms']
        for turn in self.turns:
            rows.append('| ' + str(turn['index']) + ' | ' + ' | '.join(ms(turn.get(key)) for key in fields) + ' |')
        rows += ['', '| Interruption | Requested seconds into speech | Actual seconds into speech | User onset → playback flush (ms) | User onset → last audible assistant sample (ms) | User onset → server cancel acknowledgement (ms) |',
                 '| --- | ---: | ---: | ---: | ---: | ---: |']
        for turn in self.turns:
            if 'interrupt_after_s' in turn:
                rows.append('| ' + ' | '.join([
                    str(turn['index']), str(turn['interrupt_after_s']), str(turn.get('actual_interrupt_after_s', 'n/a')),
                    ms(turn.get('interruption_to_playback_stop_ms')),
                    ms(turn.get('interruption_to_last_assistant_speech_ms')),
                    ms(turn.get('interruption_to_cancel_ack_ms'))]) + ' |')
        rows += ['', 'The final seventh step is listening through the last reply, with no new speech.', '',
                 '- ASR uses progressive rolling-prefix recognition by default (batch mode with --batch-asr). Partial hypotheses may be revised. First/last token timings observe actual Transformers streamer emissions across prefix decodes, including language and control tokens; they are not transcript-return timestamps.',
                 '- Server stage intervals use VAD packet boundaries (20 ms input packets). Audible playback uses 10 ms RMS bounds at 0.003. These are estimates rather than acoustic microphone/speaker measurements.',
                 '- The LLM first token is observed on the upstream token stream (including reasoning tokens when present). TTS first voiced output is the first generated PCM chunk with speech-level energy, before client playback.',
                 '- The benchmark buffers provisional audio until server commitment and flushes playback on detected interruptions. The browser independently gates playback using local speech detection and the configured silence gap; its microphone remains active during replies. This synthetic benchmark is not an acoustic browser barge-in test.',
                 '- The server currently treats conversation.item.truncate as a no-op; playback truncation does not guarantee that stored assistant context matches only the heard prefix.',
                 '- A negative last-audible-sample interval means the assistant was already silent at the user onset. It is not negative cancellation latency.', '',
                 '[Conversation recording](conversation.wav)', '']
        rows += ['## Incremental LLM input', '',
                 '| Step | ASR text updates sent | Prefill batches received before user speech ended |',
                 '| --- | ---: | ---: |']
        for turn in self.turns:
            rows.append(f'| {turn["index"]} | {turn.get("llm_token_updates", 0)} | {turn.get("llm_prefill_batches_before_speech_end", 0)} |')
        rows.append('')
        (self.args.output_dir / 'results.md').write_text('\n'.join(rows))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18000')
    parser.add_argument('--llm', default='unsloth/Qwen3.5-4B')
    parser.add_argument('--tts', default='qwen3-tts-1.7b-customvoice')
    parser.add_argument('--voice', default='Ryan')
    parser.add_argument('--input-tts', default='qwen3-tts-1.7b-customvoice')
    parser.add_argument('--input-voice', default='Ryan')
    parser.add_argument('--asr', default='qwen3-asr-1.7b')
    parser.add_argument('--silence-ms', type=int, default=None, help='Override the server/environment silence gap')
    parser.add_argument('--clip-dir', type=Path, default=Path(__file__).parent / 'fixtures' / 'book-interruptions')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).parent / 'results' / 'book-interruptions')
    parser.add_argument('--record-file', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--warmup', action='store_true', help='Load ASR and assistant TTS before the recorded call')
    parser.add_argument('--batch-asr', action='store_true', help='Disable progressive ASR for baseline comparisons')
    parser.add_argument('--no-llm-prefill', action='store_true', help='Use chat completions after ASR for comparison')
    parser.add_argument('--limit-turns', type=int, default=6, choices=range(1, 7), help='Shorten a diagnostic run')
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip('/')
    async with httpx.AsyncClient(timeout=180) as client:
        deadline = time.perf_counter() + 60
        while True:
            try:
                if (await client.get(args.base_url + '/health', timeout=1)).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.perf_counter() >= deadline:
                raise RuntimeError('Sound server did not become ready')
            await asyncio.sleep(.25)
        clips = await prepare_clips(args, client)
        if args.warmup and not args.prepare_only:
            response = await client.post(args.base_url + '/v1/audio/transcriptions',
                files={'file': ('warmup.wav', wav_bytes(clips[0]), 'audio/wav')},
                data={'model': args.asr, 'language': 'English'})
            response.raise_for_status()
            response = await client.post(args.base_url + '/v1/audio/speech', json={
                'model': args.tts, 'voice': args.voice, 'input': 'Ready.',
                'language': 'English', 'response_format': 'wav'})
            response.raise_for_status()
            print('WARMUP_COMPLETE', flush=True)
    if args.prepare_only:
        return
    url = args.base_url.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
    url += '/v1/realtime?' + urlencode({'model': args.tts})
    async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
        call = BookCall(args, ws)
        receiver = asyncio.create_task(call.receive())
        try:
            vad = {'type': 'server_vad', 'threshold': .005, 'create_response': True}
            if args.silence_ms is not None:
                vad['silence_duration_ms'] = args.silence_ms
            await call.send('session.update', session={
                'llm_model': args.llm, 'voice': args.voice, 'stream_tts': True,
                'stream_asr': not args.batch_asr,
                'stream_llm_input': not args.no_llm_prefill,
                'instructions': INSTRUCTIONS,
                'turn_detection': vad})
            previous = None
            for index, ((text, after), pcm) in enumerate(list(zip(STEPS, clips))[:args.limit_turns], 1):
                if previous and after is None:
                    await call.wait_finished(previous)
                turn, previous = await call.speak(index, text, pcm,
                    interruption=(previous, after) if after is not None else None)
            await call.wait_finished(previous)
            print('FINAL_LISTEN_COMPLETE', flush=True)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            call.save()


if __name__ == '__main__':
    asyncio.run(main())
