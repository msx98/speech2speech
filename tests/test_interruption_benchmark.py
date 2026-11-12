import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
import numpy as np

folder = Path(__file__).parent / 'benchmarks'
sys.path.insert(0, str(folder))
spec = importlib.util.spec_from_file_location('interruption_benchmark', folder / 'realtime_interruptions.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class ClipTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_clips_are_reused_without_tts_or_asr(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/speech'):
                pcm = (np.sin(np.arange(24000) * .1) * 5000).astype('<i2').tobytes()
                return httpx.Response(200, content=benchmark.wav_bytes(pcm))
            return httpx.Response(200, json={'text': 'Wait.'})
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(base_url='http://test', input_tts='customvoice', input_voice='Ryan',
                                      asr='asr', clip_dir=Path(temp))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with patch.object(benchmark, 'STEPS', [('wait...', 1)]):
                    first = await benchmark.prepare_clips(args, client)
                    second = await benchmark.prepare_clips(args, client)
            self.assertEqual(first, second)
            self.assertEqual(calls, ['/v1/audio/speech', '/v1/audio/transcriptions'])

    def test_speech_bounds_exclude_silence(self):
        pcm = bytes(4800) + (np.ones(2400) * 1000).astype('<i2').tobytes() + bytes(9600)
        self.assertEqual(benchmark.voiced_bounds(pcm), (2400, 4800))
        self.assertIsNone(benchmark.voiced_bounds(bytes(960)))

    def test_played_duration_excludes_buffered_audio(self):
        response = {'spans': [(0., 1., b''), (1.2, 2.2, b'')]}
        self.assertAlmostEqual(benchmark.BookCall.played_duration(response, 1.5), 1.3)

    def test_prefill_counts_exclude_empty_prompt_header(self):
        turn = {'input_started_at': 10., 'user_speech_ended_at': 12., 'first_assistant_speech_at': 13.}
        events = [
            {'type': 'llm.input_token', 'at': 10., 'revision': 1, 'text_chars': 0},
            {'type': 'llm.input_prefill', 'at': 10.1, 'revision': 1, 'batch_tokens': 30},
            {'type': 'llm.input_token', 'at': 11., 'revision': 2, 'text_chars': 3, 'observed_at': 10.9, 'sent_at': 11.},
            {'type': 'llm.input_prefill', 'at': 11.1, 'revision': 2, 'batch_tokens': 1},
        ]
        result = benchmark.prefill_metrics(turn, events)
        self.assertEqual(result['llm_token_updates'], 1)
        self.assertEqual(result['llm_prefill_batches_before_speech_end'], 1)


if __name__ == '__main__':
    unittest.main()
