"""Stream synthesized speech at speaking speed and measure realtime turn latency.

Run on rocbox with the server already running:
    .venv/bin/python src/tests/benchmarks/realtime_conversation.py

The test uses Ryan as the speaker, Angel/OmniVoice for replies, and reports
ASR transcripts plus first-audio latency relative to the end of input audio.
"""
import asyncio
import argparse
from array import array
import base64
import io
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import websockets


def voiced_bounds(pcm, threshold=0.003):
    """10 ms RMS bounds for a reproducible audible-speech estimate."""
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    active = []
    for start in range(0, len(samples), 240):
        block = samples[start:start + 240]
        if np.sqrt(np.mean(block * block)) >= threshold:
            active.append((start, start + len(block)))
    return (active[0][0], active[-1][1]) if active else None


class ConversationRecording:
    """Stereo PCM timeline: speaker left, assistant right, real pauses retained."""
    def __init__(self, path):
        self.path = path
        self.origin = None
        self.output_until = 0.0
        self.segments = []

    def input(self, pcm):
        now = time.perf_counter()
        if self.origin is None:
            self.origin = now
        if self.path:
            self.segments.append((0, now - self.origin, pcm))

    def output(self, pcm):
        now = time.perf_counter()
        if self.origin is None:
            self.origin = now
        start = max(now - self.origin, self.output_until)
        self.output_until = start + len(pcm) / 48000
        if self.path:
            self.segments.append((1, start, pcm))
        return start, self.output_until

    def interrupt_output(self):
        """Discard unplayed assistant audio, including the rest of a chunk."""
        if self.origin is None:
            return 0.0
        now = time.perf_counter() - self.origin
        kept = []
        for channel, start, pcm in self.segments:
            if channel == 0:
                kept.append((channel, start, pcm))
            elif start < now:
                size = max(0, round((now - start) * 24000)) * 2
                kept.append((channel, start, pcm[:size]))
        self.segments = kept
        self.output_until = min(self.output_until, now)
        return now

    async def finish_playback(self):
        if self.origin is not None:
            await asyncio.sleep(max(0, self.origin + self.output_until - time.perf_counter()))

    def save(self):
        if not self.path or not self.segments:
            return
        frames = max(round(at * 24000) + len(pcm) // 2 for _, at, pcm in self.segments)
        stereo = array('h', [0]) * (frames * 2)
        for channel, at, pcm in self.segments:
            samples = array('h')
            samples.frombytes(pcm)
            if sys.byteorder != 'little':
                samples.byteswap()
            offset = round(at * 24000) * 2 + channel
            for index, sample in enumerate(samples):
                stereo[offset + index * 2] = sample
        if sys.byteorder != 'little':
            stereo.byteswap()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.path), 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(stereo.tobytes())


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18000')
    parser.add_argument('--llm', default='unsloth/Qwen3.5-4B')
    parser.add_argument('--tts', default='omnivoice')
    parser.add_argument('--voice', default='profile:Angel')
    parser.add_argument('--silence-ms', type=int, default=700)
    parser.add_argument('--phrase', action='append', help='Override spoken inputs; repeat for multiple turns')
    parser.add_argument('--no-streaming', action='store_true', help='Use complete-sentence TTS as a baseline')
    parser.add_argument('--no-llm-prefill', action='store_true', help='Wait for final ASR before sending chat completions')
    parser.add_argument('--uncached-llm', action='store_true', help='Prevent complete-prompt reuse across comparison requests')
    parser.add_argument('--verify-audio', action='store_true', help='Transcribe reply audio through ASR')
    parser.add_argument('--output-dir', type=Path, help='Save reply WAVs and stage timings here')
    parser.add_argument('--input-dir', type=Path, help='Reuse saved input-turn-N.wav files for paired comparisons')
    parser.add_argument('--instructions', default='Have a friendly English conversation. Reply with one short sentence.')
    parser.add_argument('--record-file', type=Path, help='Save a stereo WAV: speaker left, assistant right, with real timing')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    phrases = [
        'Hi there. I have a little time this afternoon. Can you suggest something relaxing to do?',
        'That sounds nice. Could you give me one more idea that I can do at home?',
        'Thank you. I think I will try that today. Have a lovely afternoon.',
    ]
    if args.phrase:
        phrases = args.phrase
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    recording = ConversationRecording(args.record_file)
    # Prepare the simulated speaker before the call so generation time does
    # not appear as artificial thinking pauses in the conversation recording.
    input_wavs = []
    async with httpx.AsyncClient(timeout=180) as client:
        for turn_index, phrase in enumerate(phrases, 1):
            if args.input_dir:
                input_wav = (args.input_dir / f'input-turn-{turn_index}.wav').read_bytes()
            else:
                r = await client.post(base + '/v1/audio/speech', json={
                    'model': 'qwen3-tts-1.7b-customvoice', 'voice': 'Ryan',
                    'input': phrase, 'language': 'English', 'response_format': 'wav'})
                r.raise_for_status()
                input_wav = r.content
            input_wavs.append(input_wav)
            if args.output_dir:
                (args.output_dir / f'input-turn-{turn_index}.wav').write_bytes(input_wav)
    from urllib.parse import urlencode
    socket_url = base.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
    socket_url += '/v1/realtime?' + urlencode({'model': args.tts})
    async with websockets.connect(socket_url) as ws:
        await ws.send(json.dumps({'type': 'session.update', 'session': {
            'llm_model': args.llm, 'voice': args.voice,
            'stream_tts': not args.no_streaming,
            'stream_llm_input': not args.no_llm_prefill,
            'llm_cache_prompt': not args.uncached_llm,
            'instructions': args.instructions,
            'turn_detection': {'type': 'server_vad', 'threshold': 0.005,
                               'silence_duration_ms': args.silence_ms, 'create_response': True}}}))
        async with httpx.AsyncClient(timeout=180) as client:
            for turn_index, phrase in enumerate(phrases, 1):
                input_wav = input_wavs[turn_index - 1]
                with wave.open(io.BytesIO(input_wav)) as f:
                    assert f.getframerate() == 24000
                    pcm = f.readframes(f.getnframes())
                duration = len(pcm) / 48000
                input_voice = voiced_bounds(pcm)
                print('INPUT', phrase, 'duration', round(duration, 2), flush=True)
                started = time.perf_counter()
                async def send_audio():
                    audio = pcm + bytes(48000)
                    for i in range(0, len(audio), 960):
                        await asyncio.sleep(max(0, started + i / 48000 - time.perf_counter()))
                        chunk = audio[i:i+960]
                        recording.input(chunk)
                        await asyncio.sleep(max(0, started + (i + len(chunk)) / 48000 - time.perf_counter()))
                        await ws.send(json.dumps({'type': 'input_audio_buffer.append',
                            'audio': base64.b64encode(chunk).decode()}))
                sender = asyncio.create_task(send_audio())
                first = False
                output = bytearray()
                record = {'input': phrase, 'input_duration_s': duration, 'stream_tts': not args.no_streaming,
                          'stream_llm_input': not args.no_llm_prefill}
                record['audio_underruns_ms'] = []
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 180))
                    typ = m['type']
                    elapsed = time.perf_counter() - started
                    if typ in ('input_audio_buffer.speech_stopped',
                               'conversation.item.input_audio_transcription.completed',
                               'response.output_audio_transcript.done', 'error'):
                        print(round(elapsed, 3), typ, m.get('transcript', ''), m.get('error', ''), flush=True)
                    if typ == 'response.output_audio.delta':
                        chunk = base64.b64decode(m['delta'])
                        output.extend(chunk)
                        if first:
                            gap = time.perf_counter() - recording.origin - recording.output_until
                            if gap > 0.02:
                                record['audio_underruns_ms'].append(round(gap * 1000, 2))
                        at, _ = recording.output(chunk)
                        bounds = voiced_bounds(chunk)
                        if bounds and input_voice and 'audible_reply_gap_s' not in record:
                            first_speech = recording.origin + at + bounds[0] / 24000
                            record['audible_reply_gap_s'] = first_speech - started - input_voice[1] / 24000
                        if not first:
                            record['first_audio_s'] = elapsed
                            record['gap_after_input_s'] = elapsed - duration
                            print('FIRST_AUDIO', round(elapsed, 3), 'gap_after_input', round(elapsed-duration, 3), flush=True)
                            if elapsed < duration:
                                print('EARLY_REPLY: assistant replied before the input finished; '
                                      'gap is not a valid completed-turn latency', flush=True)
                            first = True
                    if typ == 'conversation.item.input_audio_transcription.completed':
                        record['input_transcript'] = m.get('transcript', '')
                    if typ == 'response.output_audio_transcript.done':
                        record['reply'] = m.get('transcript', '')
                    if typ == 'response.timing':
                        record['stages_ms'] = m['stages']
                        print('STAGES_MS', json.dumps(m['stages']), flush=True)
                    if typ in ('response.done', 'error'):
                        if typ == 'error' or m.get('response', {}).get('status') == 'failed':
                            raise RuntimeError(m)
                        break
                await sender
                if not first:
                    raise RuntimeError('Response completed without audio')
                if output:
                    mem = io.BytesIO()
                    with wave.open(mem, 'wb') as f:
                        f.setnchannels(1)
                        f.setsampwidth(2)
                        f.setframerate(24000)
                        f.writeframes(output)
                    if args.output_dir:
                        (args.output_dir / f'turn-{turn_index}.wav').write_bytes(mem.getvalue())
                    if args.verify_audio:
                        verification = await client.post(base + '/v1/audio/transcriptions',
                            files={'file': ('reply.wav', mem.getvalue(), 'audio/wav')},
                            data={'model': 'qwen3-asr-1.7b', 'language': 'English'})
                        verification.raise_for_status()
                        record['reply_asr'] = verification.json().get('text', '')
                        print('REPLY_ASR', record['reply_asr'], flush=True)
                    await recording.finish_playback()
                results.append(record)
                if args.output_dir:
                    (args.output_dir / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
                recording.save()
    if args.record_file:
        print('RECORDING', args.record_file, flush=True)

if __name__ == '__main__':
    asyncio.run(main())
