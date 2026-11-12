"""Persistent llama.cpp text-input session, fed by individual ASR tokens."""
import asyncio
import contextlib
import json
import time
from urllib.parse import urlencode

import websockets


class StreamingChat:
    def __init__(self, base, model, key, instructions, max_tokens, on_event):
        self.signature = (model, instructions)
        self.base, self.model, self.key = base, model, key
        self.instructions, self.max_tokens = instructions, max_tokens
        self.on_event = on_event
        self.ws = None
        self.chat_id = None
        self.revision = 0
        self.last_text = None
        self.turn_open = False
        self.generating = False
        self.queue = asyncio.Queue(maxsize=128)
        self.responses = asyncio.Queue()
        self.done = asyncio.Event()
        self.done.set()
        self.send_lock = asyncio.Lock()
        self.failure = None
        self.reader = self.writer = None
        self.supports_preview = False
        self.preview = False
        self.discarding = False
        self.discarded = asyncio.Event()
        self.accepted = asyncio.Event()
        self.control_lock = asyncio.Lock()
        self.before_update = self.on_prefill = None
        self.prefilled_revision = -1
        self.notified_revision = -1
        self.updating = False
        self.loop = asyncio.get_running_loop()

    async def send(self, event):
        async with self.send_lock:
            await self.ws.send(json.dumps(event))

    async def connect(self):
        url = self.base.rstrip('/').replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
        url += '/realtime?' + urlencode({'input': 'text', 'model': self.model})
        headers = {'Authorization': 'Bearer ' + self.key} if self.key else None
        self.ws = await websockets.connect(url, additional_headers=headers, open_timeout=30, max_size=4 * 1024 * 1024)
        try:
            event = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if event.get('type') != 'session.created' or not event.get('session', {}).get('chat_id'):
                raise RuntimeError('Upstream does not support incremental text input: ' + str(event))
            self.chat_id = event['session']['chat_id']
            self.supports_preview = event['session'].get('input_speculation', False)
            await self.send({'type': 'session.update', 'session': {
                'instructions': self.instructions, 'output_modalities': ['text'],
                'max_output_tokens': self.max_tokens}})
            event = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if event.get('type') != 'session.updated':
                raise RuntimeError(str(event))
            self.reader = asyncio.create_task(self._read())
            self.writer = asyncio.create_task(self._write())
        except BaseException:
            await self.close()
            raise
        return self

    async def _read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                typ = event.get('type')
                if typ == 'error':
                    raise RuntimeError(str(event.get('error')))
                if typ == 'input_text_buffer.prefill':
                    if event.get('finished'):
                        self.prefilled_revision = event['revision']
                    await self.on_event({'type': 'llm.input_prefill', **{k: v for k, v in event.items() if k != 'type'}})
                    await self._caught_up()
                elif typ == 'response.discarded':
                    self.preview = False
                    self.generating = False
                    self.turn_open = True
                    self.discarded.set()
                elif typ == 'response.accepted':
                    self.preview = False
                    self.accepted.set()
                elif typ == 'response.output_text.delta':
                    await self.responses.put(('token', event['delta']))
                elif typ == 'response.done':
                    self.generating = False
                    self.done.set()
                    await self.responses.put(('done', event['response']['status']))
            raise RuntimeError('Incremental LLM connection closed')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            self.done.set()
            self.discarded.set()
            self.accepted.set()
            await self.responses.put(('error', exc))

    async def _write(self):
        while True:
            text, final, observed = await self.queue.get()
            try:
                if self.failure:
                    raise self.failure
                # Re-decoding an ASR prefix repeats already known words. Keep
                # speculative prefill until a correction or new text arrives.
                if not final and self.last_text is not None and self.last_text.startswith(text):
                    continue
                if self.last_text == text:
                    continue
                self.updating = True
                if self.before_update is not None:
                    await self.before_update()
                if self.preview:
                    await self.discard()
                self.revision += 1
                self.last_text = text
                await self.send({'type': 'input_text_buffer.update', 'text': text, 'revision': self.revision})
                self.turn_open = True
                await self.on_event({'type': 'llm.input_token', 'chat_id': self.chat_id,
                    'revision': self.revision, 'observed_at': observed, 'sent_at': time.perf_counter(),
                    'text_chars': len(text)})
            except Exception as exc:
                self.failure = exc
                await self.responses.put(('error', exc))
            finally:
                self.updating = False
                self.queue.task_done()
                await self._caught_up()

    async def _caught_up(self):
        if (self.on_prefill is not None and self.last_text and self.queue.empty() and not self.updating
                and not self.generating and not self.preview and not self.discarding
                and self.prefilled_revision == self.revision and self.notified_revision != self.revision):
            self.notified_revision = self.revision
            await self.on_prefill(self.last_text, self.revision)

    async def update(self, text, final=False):
        if self.failure:
            raise self.failure
        await self.queue.put((text, final, time.perf_counter()))

    def update_from_thread(self, text):
        if self.failure:
            raise self.failure
        future = asyncio.run_coroutine_threadsafe(self.queue.put((text, False, time.perf_counter())), self.loop)
        future.result(timeout=5)

    async def begin_turn(self):
        if self.preview:
            await self.discard()
        if self.generating:
            await self.cancel()
        if not self.turn_open:
            self.last_text = None
            await self.update('', final=True)

    async def tokens(self, text, speculative=False, on_started=None):
        await self.begin_turn()
        await self.update(text, final=True)
        await self.queue.join()
        if self.failure:
            raise self.failure
        self.responses = asyncio.Queue()
        self.done.clear()
        self.generating = True
        self.preview = speculative
        self.turn_open = False
        await self.send({'type': 'response.create', 'response': {'speculative': speculative}})
        if on_started is not None:
            on_started()
        try:
            while True:
                kind, value = await self.responses.get()
                if kind == 'error':
                    raise value
                if kind == 'done':
                    if value == 'failed':
                        raise RuntimeError('Incremental LLM response failed')
                    return
                yield value
        finally:
            if self.generating:
                if self.preview:
                    await self.discard()
                else:
                    await self.cancel()

    async def discard(self):
        async with self.control_lock:
            if not self.preview:
                return
            self.discarding = True
            self.discarded.clear()
            try:
                await self.send({'type': 'response.discard'})
                await asyncio.wait_for(self.discarded.wait(), 10)
                if self.failure:
                    raise self.failure
            finally:
                self.discarding = False

    async def accept(self):
        async with self.control_lock:
            if self.preview:
                self.accepted.clear()
                await self.send({'type': 'response.accept'})
                await asyncio.wait_for(self.accepted.wait(), 10)
                if self.failure:
                    raise self.failure

    async def cancel(self):
        if self.generating and not self.failure:
            await self.send({'type': 'response.cancel'})
            await asyncio.wait_for(self.done.wait(), 10)

    async def close(self):
        self.failure = self.failure or RuntimeError('Incremental LLM session closed')
        self.done.set()
        self.discarded.set()
        self.accepted.set()
        for task in (self.writer, self.reader):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.writer, self.reader) if task), return_exceptions=True)
        if self.ws:
            with contextlib.suppress(Exception):
                await self.ws.close()
