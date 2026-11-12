"""Verify buffered speech is discarded when the user resumes within the gap."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx
import websockets

from realtime_interruptions import BookCall, INSTRUCTIONS, read_wav, voiced_bounds


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18000')
    parser.add_argument('--expected-gap-ms', type=int, default=1400)
    parser.add_argument('--pause-ms', type=int, default=1000)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--record-file', type=Path)
    parser.set_defaults(llm='unsloth/Qwen3.5-4B', tts='qwen3-tts-1.7b-customvoice', voice='Ryan',
                        silence_ms=None, batch_asr=False, no_llm_prefill=False, warmup=True)
    args = parser.parse_args()
    if args.pause_ms >= args.expected_gap_ms:
        raise ValueError('The test pause must be shorter than the silence gap')
    args.record_file = args.record_file or args.output_dir / 'conversation.wav'
    fixtures = Path(__file__).parent / 'fixtures/book-interruptions'
    manifest = json.loads((fixtures / 'manifest.json').read_text())
    clips = [read_wav((fixtures / manifest[index]['file']).read_bytes()) for index in (0, 4)]
    trimmed = []
    for pcm in clips:
        begin, end = voiced_bounds(pcm)
        trimmed.append(pcm[begin * 2:end * 2])
    audio = trimmed[0] + bytes(round(args.pause_ms * 48)) + trimmed[1]
    text = manifest[0]['text'] + ' ' + manifest[4]['text']
    async with httpx.AsyncClient(timeout=180) as client:
        reply = await client.post(args.base_url + '/v1/audio/speech', json={
            'model': args.tts, 'voice': args.voice, 'input': 'Ready.', 'language': 'English'})
        reply.raise_for_status()
    async with websockets.connect(args.base_url.replace('http://', 'ws://') + '/v1/realtime?model=' + args.tts) as ws:
        call = BookCall(args, ws)
        receiver = asyncio.create_task(call.receive())
        try:
            await call.send('session.update', session={
                'llm_model': args.llm, 'voice': args.voice, 'instructions': INSTRUCTIONS,
                'turn_detection': {'type': 'server_vad', 'threshold': .005, 'create_response': True}})
            await call.until(lambda: any(e['type'] == 'session.updated' for e in call.events))
            assert args.effective_silence_ms == args.expected_gap_ms, args.effective_silence_ms
            turn, response = await call.speak(1, text, audio)
            await call.wait_finished(response)
            generated = {e.get('generation_id') for e in call.events
                         if e['type'] == 'response.output_audio.delta' and e.get('provisional') and e.get('audio_base64_chars', 0)}
            discarded = {e.get('generation_id') for e in call.events if e['type'] == 'response.discarded'}
            committed = [e for e in call.events if e['type'] == 'response.committed']
            assert generated & discarded, 'No buffered-audio candidate was discarded'
            assert len(committed) == 1, committed
            assert committed[0]['at'] > turn['user_speech_ended_at'], 'Released a reply during the user continuation'
            for generation in generated & discarded:
                rid = call.generations[generation]
                assert not call.responses[rid]['spans'], 'Played stale speculative audio'
            assert len(turn['transcripts']) == 1, turn['transcripts']
            print('PASS: environment gap', args.effective_silence_ms, 'ms; interior pause', args.pause_ms,
                  'ms; discarded audio candidates', len(generated & discarded), '; one committed reply.', flush=True)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            call.save()


if __name__ == '__main__':
    asyncio.run(main())
