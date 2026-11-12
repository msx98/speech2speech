"""Speculative audio is sent immediately; the client owns playback gating."""
import asyncio
import time
import uuid


class Candidate:
    def __init__(self, text, revision, emit):
        self.id = 'candidate_' + uuid.uuid4().hex
        self.text, self.revision, self.emit = text, revision, emit
        self.closed = self.accepted = False
        self.first_audio = True
        self.native_started = asyncio.Event()
        self.committed = asyncio.Event()
        self.task = None
        self.failure = None
        self.response_id = None
        self.timing = {'origin': time.perf_counter(), 'stages': {}}

    async def send(self, kind, **values):
        if self.closed:
            return
        if kind == 'error' and not self.accepted:
            self.failure = values
            kind = 'response.speculation_error'
        values.update(generation_id=self.id, provisional=not self.accepted)
        if kind == 'response.output_audio.delta':
            values['replace_previous'] = self.first_audio
            self.first_audio = False
        await self.emit(kind, **values)

    def rebase(self, timing):
        offset = (self.timing['origin'] - timing['origin']) * 1000
        stages = {key: round(value + offset, 2) for key, value in self.timing['stages'].items()}
        stages.update(timing['stages'])
        self.timing['stages'].clear()
        self.timing['stages'].update(stages)
        self.timing['origin'] = timing['origin']

    async def commit(self, chat, timing):
        await self.native_started.wait()
        if self.failure or self.closed:
            raise RuntimeError('Speculative response failed before commitment')
        self.rebase(timing)
        await chat.accept()
        self.accepted = True
        await self.emit('response.committed', response_id=self.response_id,
                        generation_id=self.id, provisional=False)
        self.committed.set()
