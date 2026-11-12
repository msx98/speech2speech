"""GPU acceptance for incremental llama text input and revision correctness."""
import asyncio
import json
import time

import websockets
import httpx

BASE = 'http://rocbox:18080'


async def session(label, updates):
    async with websockets.connect(BASE.replace('http://', 'ws://') + '/v1/realtime?input=text') as ws:
        greeting = json.loads(await ws.recv())
        assert greeting['type'] == 'session.created', greeting
        chat_id = greeting['session']['chat_id']
        await ws.send(json.dumps({'type': 'session.update', 'session': {
            'instructions': 'Follow the user request precisely and answer briefly.',
            'output_modalities': ['text'], 'max_output_tokens': 80}}))
        assert json.loads(await ws.recv())['type'] == 'session.updated'
        prefills = []
        generation_allowed = False
        async def read_until(predicate):
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), 60))
                assert event['type'] != 'error', event
                assert generation_allowed or not event['type'].startswith('response.'), event
                if event['type'] == 'input_text_buffer.prefill':
                    prefills.append(event)
                if predicate(event):
                    return event
        for revision, text in enumerate(updates, 1):
            await ws.send(json.dumps({'type': 'input_text_buffer.update', 'revision': revision, 'text': text}))
            await read_until(lambda e: e['type'] == 'input_text_buffer.prefill' and e['revision'] == revision and e['finished'])
        # No response may have appeared while collecting prefill acknowledgements.
        generation_allowed = True
        await ws.send(json.dumps({'type': 'response.create'}))
        output = ''
        while True:
            event = await read_until(lambda e: e['type'] in ('response.output_text.delta', 'response.done'))
            if event['type'] == 'response.done':
                assert event['response']['status'] == 'completed', event
                break
            output += event['delta']
        print(label, chat_id, repr(output), 'batches', len(prefills), flush=True)
        assert 'blue' in output.lower() and 'red' not in output.lower(), output
        generation_allowed = False
        # Keep the same chat and confirm its generated history is usable.
        await ws.send(json.dumps({'type': 'input_text_buffer.update', 'revision': 100,
                                 'text': 'Repeat the color you just answered, in one word.'}))
        await read_until(lambda e: e['type'] == 'input_text_buffer.prefill' and e['revision'] == 100 and e['finished'])
        generation_allowed = True
        await ws.send(json.dumps({'type': 'response.create'}))
        second = ''
        while True:
            event = await read_until(lambda e: e['type'] in ('response.output_text.delta', 'response.done'))
            if event['type'] == 'response.done':
                break
            second += event['delta']
        assert 'blue' in second.lower(), second
        print(label, 'second turn', repr(second), flush=True)
        return chat_id


async def pending_batches():
    async with websockets.connect(BASE.replace('http://', 'ws://') + '/v1/realtime?input=text') as ws:
        assert json.loads(await ws.recv())['type'] == 'session.created'
        await ws.send(json.dumps({'type': 'session.update', 'session': {
            'instructions': 'Answer with only the requested color.', 'output_modalities': ['text'], 'max_output_tokens': 16}}))
        assert json.loads(await ws.recv())['type'] == 'session.updated'
        prefix = 'Background words: ' + 'item ' * 1024
        await ws.send(json.dumps({'type': 'input_text_buffer.update', 'revision': 1,
                                 'text': prefix + '\nReply with only the word red.'}))
        batches = []
        updated = False
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 60))
            assert event['type'] != 'error', event
            assert not event['type'].startswith('response.'), event
            if event['type'] != 'input_text_buffer.prefill':
                continue
            if event['batch_tokens']:
                batches.append(event)
            if not updated and event['revision'] == 1 and not event['finished']:
                await ws.send(json.dumps({'type': 'input_text_buffer.update', 'revision': 2,
                                         'text': prefix + '\nReply with only the word blue.'}))
                updated = True
            if event['revision'] == 2 and event['finished']:
                break
        assert updated and len(batches) > 2
        first_updated = next(i for i, e in enumerate(batches) if e['revision'] == 2)
        assert first_updated < len(batches) - 1, 'Update was not consumed between prefill batches'
        gaps = [b['batch_start_us'] - a['batch_end_us'] for a, b in zip(batches, batches[1:])]
        print('Mid-prefill update:', len(batches), 'batches, new revision at batch', first_updated + 1,
              'scheduler gaps us', min(gaps), max(gaps), flush=True)
        await ws.send(json.dumps({'type': 'response.create'}))
        text = ''
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 60))
            assert event['type'] != 'error', event
            if event['type'] == 'response.output_text.delta':
                text += event['delta']
            if event['type'] == 'response.done':
                break
        assert 'blue' in text.lower() and 'red' not in text.lower(), text


async def speculative_rollback():
    async with websockets.connect(BASE.replace('http://', 'ws://') + '/v1/realtime?input=text') as ws:
        greeting = json.loads(await ws.recv())
        assert greeting['session']['input_speculation']
        async def send(kind, **values):
            await ws.send(json.dumps({'type': kind, **values}))
        async def until(predicate):
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), 60))
                assert event['type'] != 'error', event
                if predicate(event): return event
        await send('session.update', session={'instructions': 'Reply with only the requested color.',
                                              'output_modalities': ['text'], 'max_output_tokens': 16})
        await until(lambda e: e['type'] == 'session.updated')
        await send('input_text_buffer.update', revision=1, text='Reply only red.')
        before = await until(lambda e: e['type'] == 'input_text_buffer.prefill' and e['revision'] == 1 and e['finished'])
        await send('response.create', response={'speculative': True})
        await until(lambda e: e['type'] == 'response.output_text.delta')
        await send('response.discard')
        await until(lambda e: e['type'] == 'response.discarded')
        restored = await until(lambda e: e['type'] == 'input_text_buffer.prefill' and e['revision'] == 1 and e['finished'])
        assert restored['total_tokens'] == before['total_tokens'], (before, restored)
        await send('input_text_buffer.update', revision=2, text='Reply only blue.')
        await until(lambda e: e['type'] == 'input_text_buffer.prefill' and e['revision'] == 2 and e['finished'])
        await send('response.create', response={'speculative': True})
        text = ''
        while True:
            event = await until(lambda e: e['type'] in ('response.output_text.delta', 'response.done'))
            if event['type'] == 'response.done': break
            text += event['delta']
        assert 'blue' in text.lower() and 'red' not in text.lower(), text
        await send('response.accept')
        await until(lambda e: e['type'] == 'response.accepted')
        await send('input_text_buffer.update', revision=3, text='Repeat that color.')
        await until(lambda e: e['type'] == 'input_text_buffer.prefill' and e['revision'] == 3 and e['finished'])
        await send('response.create')
        text = ''
        while True:
            event = await until(lambda e: e['type'] in ('response.output_text.delta', 'response.done'))
            if event['type'] == 'response.done': break
            text += event['delta']
        assert 'blue' in text.lower(), text
        print('Speculative discard restored exact input length; accepted preview retained across turns.', flush=True)


async def main():
    async with httpx.AsyncClient(timeout=1) as client:
        deadline = time.perf_counter() + 60
        while True:
            try:
                if (await client.get(BASE + '/health')).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.perf_counter() >= deadline:
                raise RuntimeError('llama test server did not become ready')
            await asyncio.sleep(.25)
    ids = await asyncio.gather(
        session('correction', ['Reply with only the word red.', 'Reply with only the word blue.']),
        session('token-boundaries', ['Reply with only the word b', 'Reply with only the word bl',
                                     'Reply with only the word blu', 'Reply with only the word blue.']))
    assert ids[0] != ids[1]
    await pending_batches()
    await speculative_rollback()
    print('Revision, BPE boundary, multi-turn, and concurrent chat isolation checks passed.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
