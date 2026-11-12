// Requires localhost:18000 and Chromium CDP :9222 with a spoken WAV supplied
// through --use-file-for-fake-audio-capture (see fixtures/book-interruptions).
const pages = await (await fetch('http://localhost:9222/json')).json();
const socket = new WebSocket(pages[0].webSocketDebuggerUrl);
await new Promise(resolve => { socket.onopen = resolve; });
let id = 0;
const pending = new Map(), errors = [];
socket.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.id) { pending.get(message.id)?.(message.result); pending.delete(message.id); }
  else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params);
};
function call(method, params = {}) {
  return new Promise(resolve => {
    pending.set(++id, resolve); socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(expression, timeout = 90000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await evaluate(expression)) return; await wait(200); }
  throw new Error('Timed out: ' + expression);
}
try {
  await call('Runtime.enable'); await call('Page.enable');
  await call('Page.navigate', { url: 'http://localhost:18000/' });
  await until(`!!document.getElementById('record-clip-btn') && typeof loadClips === 'function'`);
  await evaluate(`document.querySelector('[data-panel="config"]').click()`);
  const before = await evaluate(`(async()=> (await (await fetch('/tts/config/list')).json()).clips.map(c=>c.name))()`);
  await evaluate(`document.getElementById('record-clip-btn').click()`);
  await until(`document.getElementById('record-clip-btn').getAttribute('aria-pressed') === 'true'`);
  if (!await evaluate(`document.getElementById('upload-btn').disabled && document.getElementById('call-start').disabled`))
    throw new Error('Conflicting controls stayed enabled');
  await wait(4100);
  await evaluate(`document.getElementById('record-clip-btn').click()`);
  await until(`document.getElementById('record-clip-status').textContent === 'Recording saved and transcribed.'`);
  const clips = await evaluate(`(async()=> (await (await fetch('/tts/config/list')).json()).clips)()`);
  const added = clips.filter(clip => !before.includes(clip.name));
  if (added.length !== 1 || !added[0].txt_content?.trim()) throw new Error('Expected one transcribed clip: ' + JSON.stringify(added));
  const clip = added[0];
  if (!await evaluate(`(async()=> (await fetch('/tts/config/wav/' + ${JSON.stringify(clip.name)})).ok)()`))
    throw new Error('Saved recording audio unavailable');
  if (!await evaluate(`!clipStream && !clipRecorder && !document.getElementById('upload-btn').disabled`))
    throw new Error('Microphone or controls were not released');
  console.log('PASS saved browser recording', JSON.stringify(clip));
  // Permission denial must restore controls and leave the UI ready to retry.
  await evaluate(`navigator.mediaDevices.getUserMedia = async()=>{throw new DOMException('Denied','NotAllowedError')}; document.getElementById('record-clip-btn').click()`);
  await until(`document.getElementById('record-clip-status').textContent.includes('denied')`);
  if (!await evaluate(`!document.getElementById('record-clip-btn').disabled && !document.getElementById('upload-btn').disabled`))
    throw new Error('Permission denial left controls locked');
  if (errors.length) throw new Error(JSON.stringify(errors));
  console.log('PASS permission denial recovery');
} finally { socket.close(); }
