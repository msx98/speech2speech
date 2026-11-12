import asyncio
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_asr import LiveASR


class LiveASRTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_audio_preempts_speculative_work_before_inference(self):
        order = []
        async def before(): order.append('cancel-speculation')
        async def partial(text): order.append('partial')
        def recognize(pcm, token):
            order.append('asr')
            return 'text'
        live = LiveASR(recognize, partial, before_recognize=before)
        await live.finish(bytes(100), 100)
        self.assertEqual(order, ['cancel-speculation', 'asr', 'partial'])

    async def test_partial_before_finish_and_reuse_final_hypothesis(self):
        sizes = []
        ready = asyncio.Event()
        async def partial(text):
            ready.set()
        def recognize(pcm, token):
            sizes.append(len(pcm))
            token('start'); token('token'); token('end')
            return 'recognized words'
        live = LiveASR(recognize, partial, rate=10, interval_ms=100)
        live.submit(bytes(8))
        await asyncio.wait_for(ready.wait(), 2)
        self.assertFalse(live.finishing)
        self.assertIn('asr_first_token', live.raw_timings)
        self.assertEqual(await live.finish(bytes(12), 8), 'recognized words')
        self.assertEqual(sizes, [8])

    async def test_pending_prefixes_are_coalesced(self):
        sizes = []
        started, release = threading.Event(), threading.Event()
        async def partial(text):
            pass
        def recognize(pcm, token):
            sizes.append(len(pcm))
            if len(sizes) == 1:
                started.set()
                release.wait(2)
            return str(len(pcm))
        live = LiveASR(recognize, partial, rate=10, interval_ms=100)
        live.submit(bytes(2))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        live.submit(bytes(4)); live.submit(bytes(6)); live.submit(bytes(8))
        release.set()
        self.assertEqual(await live.finish(bytes(8), 8), '8')
        self.assertEqual(sizes, [2, 8])

    async def test_short_final_tail_is_recognized(self):
        async def partial(text):
            pass
        live = LiveASR(lambda pcm, token: 'wait', partial, rate=10, interval_ms=800)
        live.submit(bytes(8))
        self.assertIsNone(live.task)
        self.assertEqual(await live.finish(bytes(8), 8), 'wait')

    async def test_inference_error_propagates(self):
        async def partial(text):
            pass
        def fail(pcm, token):
            raise ValueError('ASR unavailable')
        live = LiveASR(fail, partial, rate=10, interval_ms=100)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            await live.finish(bytes(8), 8)
        self.assertTrue(live.closed.is_set())


if __name__ == '__main__':
    unittest.main()
