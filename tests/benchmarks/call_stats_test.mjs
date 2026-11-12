import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import assert from 'node:assert/strict';
const summary = {}, chart = {};
const window = {};
runInNewContext(await readFile(new URL('../../static/call-stats.js', import.meta.url), 'utf8'), {
  window, document: { getElementById: id => id === 'call-stats-averages' ? summary : chart }
});
const stats = window.callStatistics;
const emit = (type, values, at) => stats.event({ type, ...values }, at);
emit('llm.input_prefill', { chat_id:'a', revision:1, batch_tokens:64, finished:false }, 10);
emit('llm.input_prefill', { chat_id:'a', revision:1, batch_tokens:12, finished:true }, 100);
emit('response.created', { llm_chat_id:'a', input_revision:1, response:{id:'r'} }, 110);
emit('response.chunk_ready', { response_id:'r' }, 150);
emit('response.chunk_ready', { response_id:'r' }, 180);
emit('response.audio_decoded', { response_id:'r' }, 250);
stats.audioReady({ response_id:'r' }, 270);
stats.audioReady({ response_id:'r' }, 900);
assert.match(summary.textContent, /text chunk: 50 ms \(n=1\)/);
assert.match(summary.textContent, /decoded audio: 150 ms \(n=1\)/);
assert.match(summary.textContent, /browser-ready audio: 170 ms \(n=1\)/);
assert.equal((chart.innerHTML.match(/<circle /g) || []).length, 3);
assert.match(chart.innerHTML, /Batch 2: ready 170 ms/);
emit('response.discarded', { response_id:'r' }, 300);
emit('llm.input_prefill', { chat_id:'a', revision:1, batch_tokens:0, finished:true }, 310);
assert.match(summary.textContent, /discarded: 1/);
assert.match(summary.textContent, /browser-ready audio: 170 ms \(n=1\)/);
// Revision numbers on another chat cannot attach to the first chat's sample.
emit('response.created', { llm_chat_id:'b', input_revision:1, response:{id:'other'} }, 320);
stats.audioReady({ response_id:'other' }, 999);
assert.match(summary.textContent, /browser-ready audio: 170 ms \(n=1\)/);
for(let revision=2;revision<=205;revision++)
  emit('llm.input_prefill', { chat_id:'a', revision, batch_tokens:1, finished:true }, 1000+revision);
assert.match(summary.textContent, /browser-ready audio: —/);
stats.audioReady({ response_id:'r' }, 5000);
assert.match(summary.textContent, /browser-ready audio: —/);
stats.reset();
assert.equal(summary.textContent, 'No completed batch measurements yet.');
emit('response.created', { llm_chat_id:'race', input_revision:1, response:{id:'early'} }, 10);
emit('llm.input_prefill', { chat_id:'race', revision:1, batch_tokens:3, finished:true }, 100);
emit('response.chunk_ready', { response_id:'early' }, 150);
emit('response.audio_decoded', { response_id:'early' }, 200);
stats.audioReady({ response_id:'early' }, 220);
assert.match(summary.textContent, /text chunk: 50 ms \(n=1\)/);
assert.match(summary.textContent, /browser-ready audio: 120 ms \(n=1\)/);
console.log('PASS batch association, averages, first-chunk deduplication, discard, bounded history and reset');
