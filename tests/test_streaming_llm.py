import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming_llm import StreamingChat


class Connection:
    def __init__(self):
        self.sent = []
    async def send(self, raw):
        self.sent.append(json.loads(raw))
    async def close(self):
        pass


class StreamingInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_prefixes_corrections_and_final_deletions(self):
        events = []
        async def report(event):
            events.append(event)
        chat = StreamingChat('http://test/v1', 'model', '', 'instructions', 32, report)
        chat.ws = Connection()
        chat.writer = asyncio.create_task(chat._write())
        try:
            await chat.begin_turn()
            await chat.queue.join()
            await asyncio.to_thread(chat.update_from_thread, 'Could')
            await chat.update('Could you')
            await chat.queue.join()
            await chat.update('Could')
            await chat.update('Could we')
            await chat.update('Could', final=True)
            await chat.queue.join()
            self.assertEqual([e['text'] for e in chat.ws.sent], ['', 'Could', 'Could you', 'Could we', 'Could'])
            self.assertEqual([e['revision'] for e in chat.ws.sent], [1, 2, 3, 4, 5])
            self.assertEqual(len(events), 5)
        finally:
            await chat.close()

    async def test_closed_session_rejects_new_asr_tokens(self):
        async def report(event):
            pass
        chat = StreamingChat('http://test/v1', 'model', '', 'instructions', 32, report)
        await chat.close()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await asyncio.to_thread(chat.update_from_thread, 'late token')


if __name__ == '__main__':
    unittest.main()
