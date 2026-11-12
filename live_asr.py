"""Bounded rolling-prefix recognition for the Transformers ASR backend.

One inference runs at a time; only the newest pending audio prefix is kept.
Hypotheses can be revised. This is progressive re-decoding, not a native
streaming KV-cache implementation (the vendor offers that only via vLLM).
"""
import asyncio
import threading
import time


class RecognitionCancelled(Exception):
    pass


class LiveASR:
    def __init__(self, recognize, on_partial, rate=24000, interval_ms=800, before_recognize=None, after_recognize=None):
        self.recognize, self.on_partial = recognize, on_partial
        self.before_recognize = before_recognize
        self.after_recognize = after_recognize
        self.interval_bytes = max(1, round(rate * interval_ms / 1000)) * 2
        self.pending = None
        self.active_size = 0
        self.submitted_size = 0
        self.result_size = 0
        self.text = ''
        self.raw_timings = {}
        self.wake = asyncio.Event()
        self.closed = threading.Event()
        self.finishing = False
        self.required_size = 0
        self.task = None

    def token(self, kind):
        if self.closed.is_set():
            raise RecognitionCancelled()
        now = time.perf_counter()
        if kind == 'start':
            self.raw_timings.setdefault('asr_generation_started', now)
        elif kind == 'token':
            self.raw_timings.setdefault('asr_first_token', now)
            self.raw_timings['asr_last_token'] = now
        elif kind == 'end':
            self.raw_timings['asr_tokens_done'] = now

    def submit(self, pcm):
        if self.finishing or self.closed.is_set():
            return
        if len(pcm) - self.submitted_size < self.interval_bytes:
            return
        self.pending = bytes(pcm)
        self.submitted_size = len(pcm)
        self._start()

    def _start(self):
        self.wake.set()
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closed.is_set():
            await self.wake.wait()
            self.wake.clear()
            if self.pending is None:
                if self.finishing:
                    return self.text
                continue
            pcm, self.pending = self.pending, None
            self.active_size = len(pcm)
            self.raw_timings.setdefault('asr_started', time.perf_counter())
            try:
                if self.before_recognize is not None:
                    await self.before_recognize()
                text = await asyncio.to_thread(self.recognize, pcm, self.token)
            finally:
                if self.after_recognize is not None:
                    await self.after_recognize()
            self.active_size = 0
            if self.closed.is_set():
                return self.text
            previous = self.text
            self.text, self.result_size = text.strip(), len(pcm)
            if self.text and self.text != previous:
                await self.on_partial(self.text)
            if self.finishing and self.result_size >= self.required_size:
                self.pending = None
                return self.text
            if self.pending is not None:
                self.wake.set()
        return self.text

    async def finish(self, pcm, last_voice_size):
        self.finishing = True
        self.required_size = last_voice_size or len(pcm)
        if self.active_size >= self.required_size:
            self.pending = None
        elif self.result_size >= self.required_size and not self.active_size:
            self.pending = None
        else:
            self.pending = bytes(pcm)
        self._start()
        try:
            return await self.task
        finally:
            self.closed.set()

    async def close(self):
        self.closed.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
