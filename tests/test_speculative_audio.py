import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speculative_audio import Candidate


class SpeculativeAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_chunk_replaces_then_continues(self):
        events = []
        async def emit(kind, **values): events.append((kind, values))
        candidate = Candidate('prefix', 3, emit)
        await candidate.send('response.output_audio.delta', delta='first')
        await candidate.send('response.output_audio.delta', delta='next')
        self.assertTrue(events[0][1]['replace_previous'])
        self.assertFalse(events[1][1]['replace_previous'])
        self.assertEqual(events[0][1]['generation_id'], events[1][1]['generation_id'])
        self.assertTrue(events[0][1]['provisional'])
        candidate.closed = True
        await candidate.send('response.output_audio.delta', delta='stale')
        self.assertEqual(len(events), 2)

    async def test_commit_rebases_timings_and_accepts_native_draft(self):
        events = []
        async def emit(kind, **values): events.append((kind, values))
        class Chat:
            accepted = False
            async def accept(self): self.accepted = True
        chat = Chat()
        candidate = Candidate('prefix', 1, emit)
        candidate.timing['origin'] = 10.
        candidate.timing['stages']['tts_first_audio'] = 200.
        candidate.native_started.set()
        await candidate.commit(chat, {'origin': 11., 'stages': {'speech_end': 0.}})
        self.assertTrue(chat.accepted and candidate.committed.is_set())
        self.assertEqual(candidate.timing['stages']['tts_first_audio'], -800.)
        self.assertEqual(events[0][0], 'response.committed')


if __name__ == '__main__':
    unittest.main()
