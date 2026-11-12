"""Exercise paired-reference server echo filtering without model inference."""
import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

import base64
import numpy as np
import websockets


def clip(name):
    with wave.open(str(Path(__file__).parent / 'fixtures/book-interruptions' / name), 'rb') as source:
        assert source.getframerate() == 24000 and source.getnchannels() == 1
        return np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32) / 32768


def pcm(samples):
    return (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()


async def scenario(url, reference, microphone):
    events = []
    async with websockets.connect(url.replace('http://', 'ws://') + '/v1/realtime') as ws:
        await ws.recv()
        await ws.send(json.dumps({'type': 'session.update', 'session': {
            'stream_llm_input': False, 'stream_asr': False,
            'echo_cancellation': {'enabled': True},
            # Activity classification is tested without triggering ASR inference.
            'turn_detection': {'type': 'none', 'threshold': .005}}}))
        session = json.loads(await ws.recv())
        assert session['type'] == 'session.updated', session
        assert session['session']['echo_cancellation']['enabled'], session
        async def receive():
            async for raw in ws:
                event = json.loads(raw)
                assert event['type'] != 'error', event
                events.append(event)
        reader = asyncio.create_task(receive())
        try:
            started = time.perf_counter()
            for start in range(0, len(reference), 512):
                await ws.send(json.dumps({'type': 'input_audio_buffer.append',
                    'audio': base64.b64encode(pcm(microphone[start:start+512])).decode(),
                    'playback_reference': base64.b64encode(pcm(reference[start:start+512])).decode(),
                    'captured_at': start / 24}))
                await asyncio.sleep(.001)
            expected = (len(reference) + 511) // 512
            deadline = time.perf_counter() + 20
            while sum(e['type'] == 'input_audio_buffer.activity' for e in events) < expected:
                if reader.done(): await reader
                if time.perf_counter() > deadline: raise TimeoutError('Missing clean microphone activity')
                await asyncio.sleep(.01)
            duration = time.perf_counter() - started
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    return [e for e in events if e['type'] == 'input_audio_buffer.activity'], duration


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://localhost:18000')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    far = clip('01-10da956148cc.wav')
    reference = np.tile(far, 2)
    delay = round(.08 * 24000)
    echo = np.zeros_like(reference)
    echo[delay:] = .6 * reference[:-delay]
    near = np.zeros_like(reference)
    speech = clip('05-c13e948c7a18.wav')
    onset = 3 * 24000
    near[onset:onset+len(speech)] = speech
    pure, pure_elapsed = await scenario(args.base_url, reference, echo)
    mixed, mixed_elapsed = await scenario(args.base_url, reference, echo + near)
    pure_false_voice = sum(e['voiced'] for e in pure)
    voiced_overlap = [e for e in mixed if onset / 24 <= e['captured_at'] < (onset + len(speech)) / 24 and e['voiced']]
    assert pure_false_voice == 0, f'Pure echo generated {pure_false_voice} voice detections'
    assert len(voiced_overlap) > 10, 'Simultaneous user speech was suppressed'
    result = {'sample_rate': 24000, 'echo_delay_ms': 80, 'echo_gain': .6,
              'pure_echo_voice_detections': pure_false_voice,
              'overlapping_user_voice_detections': len(voiced_overlap),
              'first_user_detection_ms_after_clip_start': voiced_overlap[0]['captured_at'] - onset / 24,
              'pure_elapsed_s': pure_elapsed, 'mixed_elapsed_s': mixed_elapsed,
              'measurement': 'Synthetic real speech mixed with a delayed/scaled echo, through actual WebSocket server filtering. Not a physical room recording.'}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
