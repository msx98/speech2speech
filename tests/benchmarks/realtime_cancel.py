"""Verify cancellation during incremental TTS and model reuse in one session."""
import asyncio
import json
import time

import websockets


async def main():
    async with websockets.connect('ws://127.0.0.1:18000/v1/realtime?model=qwen3-tts-1.7b-customvoice') as ws:
        async def send(kind, **values):
            await ws.send(json.dumps({'type': kind, **values}))

        await send('session.update', session={
            'voice': 'Ryan', 'llm_model': 'unsloth/Qwen3.5-4B',
            'instructions': 'Repeat exactly: Take a comfortable seat and enjoy a warm cup of tea while reading your favorite book this afternoon.'})
        await send('conversation.item.create', item={'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'Please begin.'}]})
        await send('response.create')
        cancelled_id = None
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if event['type'] == 'error':
                raise RuntimeError(event)
            if event['type'] == 'response.output_audio.delta':
                cancelled_id = event['response_id']
                started = time.perf_counter()
                await send('response.cancel')
                break
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if event['type'] == 'response.done' and event['response']['id'] == cancelled_id:
                assert event['response']['status'] == 'cancelled', event
                print('Cancellation acknowledged in', round(time.perf_counter() - started, 3), 'seconds', flush=True)
                break
        await send('session.update', session={'instructions': 'Reply with exactly: Hello again.'})
        await send('response.create')
        audio = 0
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if event['type'] == 'error':
                raise RuntimeError(event)
            if event['type'] == 'response.output_audio.delta':
                assert event['response_id'] != cancelled_id, 'Cancelled response leaked audio'
                audio += 1
            if event['type'] == 'response.done':
                assert event['response']['status'] == 'completed', event
                assert audio > 0
                print('Model reuse passed:', audio, 'audio chunks', flush=True)
                break


asyncio.run(main())
