"""Alternate prefill off/on using identical input and fixed reply text."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

import httpx


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18000')
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    folder = Path(__file__).parent
    manifest = json.loads((folder / 'fixtures/book-interruptions/manifest.json').read_text())
    source = folder / 'fixtures/book-interruptions' / manifest[0]['file']
    inputs = args.output_dir / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / 'input-turn-1.wav').write_bytes(source.read_bytes())
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
        response = await client.post(args.base_url + '/v1/audio/transcriptions',
            files={'file': ('warmup.wav', source.read_bytes(), 'audio/wav')},
            data={'model': 'qwen3-asr-1.7b', 'language': 'English'})
        response.raise_for_status()
        response = await client.post(args.base_url + '/v1/audio/speech', json={
            'model': 'qwen3-tts-1.7b-customvoice', 'voice': 'Ryan', 'input': 'Ready.',
            'language': 'English', 'response_format': 'wav'})
        response.raise_for_status()
    results = []
    for index, enabled in enumerate((False, True, True, False, False, True), 1):
        output = args.output_dir / f'{index}-{"on" if enabled else "off"}'
        command = [sys.executable, str(folder / 'realtime_conversation.py'),
            '--base-url', args.base_url, '--tts', 'qwen3-tts-1.7b-customvoice', '--voice', 'Ryan',
            '--input-dir', str(inputs), '--output-dir', str(output), '--record-file', str(output / 'conversation.wav'),
            '--verify-audio', '--uncached-llm', '--phrase', manifest[0]['text'],
            '--instructions', 'Reply with exactly this sentence and nothing else: Try reading The Hobbit with a warm cup of tea this afternoon.']
        if not enabled:
            command.append('--no-llm-prefill')
        process = await asyncio.create_subprocess_exec(*command)
        if await process.wait():
            raise RuntimeError(f'Comparison run {index} failed')
        record = json.loads((output / 'results.json').read_text())[0]
        results.append(record)
        (args.output_dir / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('RUN', index, 'on' if enabled else 'off', record['audible_reply_gap_s'], flush=True)
    summary = {}
    for enabled in (False, True):
        rows = [r for r in results if r['stream_llm_input'] == enabled]
        def average_stage(name):
            return statistics.mean(r['stages_ms'][name] for r in rows)
        summary['on' if enabled else 'off'] = {
            'audible_reply_gap_ms': statistics.mean(r['audible_reply_gap_s'] * 1000 for r in rows),
            'llm_first_token_after_speech_end_ms': average_stage('llm_first_token'),
            'text_ready_after_speech_end_ms': average_stage('first_text_chunk'),
            'tts_first_speech_after_speech_end_ms': average_stage('tts_first_speech'),
            'tts_generation_to_first_audio_ms': statistics.mean(
                r['stages_ms']['tts_first_audio'] - r['stages_ms']['tts_started'] for r in rows)}
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print('SUMMARY', json.dumps(summary), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
