// Requires a test server on localhost:18000 and Chromium with CDP on :9222.
// --speech uses a persistent spoken fixture to verify real batch statistics.
import { readFile } from 'node:fs/promises';
// Launch Chromium with --use-fake-ui-for-media-stream,
// --use-fake-device-for-media-stream, --autoplay-policy=no-user-gesture-required.
const pages=await (await fetch('http://localhost:9222/json')).json();
const ws=new WebSocket(pages[0].webSocketDebuggerUrl);await new Promise(r=>ws.onopen=r);
let id=0;const pending=new Map();const errors=[];
ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){pending.get(m.id)?.(m.result);pending.delete(m.id)}else if(m.method==='Runtime.exceptionThrown'){errors.push(m.params);console.log('ERROR',JSON.stringify(m.params));}};
function call(method,params={}){return new Promise(r=>{pending.set(++id,r);ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){return call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});}
await call('Runtime.enable');await call('Page.enable');
await call('Page.addScriptToEvaluateOnNewDocument',{source:`
window.callFrames=[];
window.audioActions=[];
for (const action of ['start', 'stop']) {
 const native = AudioBufferSourceNode.prototype[action];
 AudioBufferSourceNode.prototype[action] = function(...args) {
  audioActions.push({action, length:this.buffer?.length, at:performance.now()});
  return native.apply(this,args);
 };
}
const NativeSocket=window.WebSocket;
window.WebSocket=class extends NativeSocket {
 constructor(...args){super(...args);if(String(args[0]).includes('/v1/realtime')){window.testCallSocket=this;this.addEventListener('message',e=>window.callFrames.push(JSON.parse(e.data)));}}
};
`});
await call('Page.navigate',{url:'http://localhost:18000/'});
await new Promise(r=>setTimeout(r,3000));
console.log('start',JSON.stringify(await evaluate(`
document.getElementById('chat-llm-model').value='unsloth/Qwen3.5-4B';
document.getElementById('chat-model').value='qwen3-tts-1.7b-customvoice';
document.getElementById('chat-voice').value='none';
document.getElementById('call-start').click();
'clicked'
`)));
await new Promise(r=>setTimeout(r,3000));
console.log('connected',JSON.stringify(await evaluate(`JSON.stringify({status:document.getElementById('call-status').textContent,events:callFrames.map(e=>e.type),session:callFrames.find(e=>e.type==='session.updated')?.session})`)));
console.log('mute',JSON.stringify(await evaluate(`document.getElementById('call-mute').click();document.getElementById('call-mute').getAttribute('aria-pressed')`)));
if (process.argv.includes('--speech')) {
 await evaluate(`testCallSocket.send(JSON.stringify({type:'session.update',session:{instructions:'Reply with only Hello.',max_response_output_tokens:16}}))`);
 const wav=await readFile(process.env.CALL_SPEECH_FIXTURE || new URL('./fixtures/book-interruptions/06-e3aa380294e1.wav',import.meta.url));
 let pcm;
 for(let offset=12;offset+8<=wav.length;){
  const size=wav.readUInt32LE(offset+4);
  if(wav.toString('ascii',offset,offset+4)==='data'){pcm=wav.subarray(offset+8,offset+8+size);break;}
  offset+=8+size+(size%2);
 }
 if(!pcm) throw new Error('Missing WAV data');
 await evaluate(`(async()=>{
  const raw=atob(${JSON.stringify(pcm.toString('base64'))})+String.fromCharCode(0).repeat(96000);
  for(let offset=0;offset<raw.length;offset+=960){
   testCallSocket.send(JSON.stringify({type:'input_audio_buffer.append',audio:btoa(raw.slice(offset,offset+960))}));
   await new Promise(r=>setTimeout(r,20));
  }
 })()`);
} else {
 await evaluate(`testCallSocket.send(JSON.stringify({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:'Reply with only Hello.'}]}}));testCallSocket.send(JSON.stringify({type:'response.create'}));`);
}
for(let i=0;i<45;i++){
 await new Promise(r=>setTimeout(r,1000));
 const result=await evaluate(`JSON.stringify({done:callFrames.some(e=>e.type==='response.done'),audio:callFrames.filter(e=>e.type==='response.output_audio.delta').length,errors:callFrames.filter(e=>e.type==='error'),status:document.getElementById('call-status').textContent})`);
 const data=JSON.parse(result.result.value);
 if(data.done||data.errors.length){
   console.log('reply',data);
   if (!data.done || !data.audio || data.errors.length) throw new Error('Call response failed');
   break;
 }
}
for (let i=0;i<30;i++) {
 if ((await evaluate('callFrames.some(e=>e.type==="response.timing" && e.stages.browser_receive_to_playback !== undefined)')).result.value) break;
 await new Promise(r=>setTimeout(r,100));
}
console.log('timings',JSON.stringify(await evaluate('JSON.stringify(callFrames.filter(e=>e.type==="response.timing"))')));
const metrics=await evaluate('callFrames.some(e=>e.type==="response.timing" && e.stages.browser_receive_to_playback !== undefined)');
if (!metrics.result.value) throw new Error('Browser playback timing was not acknowledged');
if(process.argv.includes('--speech')) {
 const statistics=await evaluate(`JSON.stringify({summary:document.getElementById('call-stats-averages').textContent,points:document.querySelectorAll('#call-stats-chart circle').length,events:callFrames.filter(e=>['response.created','response.chunk_ready','response.audio_decoded','llm.input_prefill'].includes(e.type))})`);
 const data=JSON.parse(statistics.result.value);
 if(data.points<3 || !/browser-ready audio: \d+ ms/.test(data.summary)) throw new Error('Missing real batch measurements: '+JSON.stringify(data));
 console.log('real batch statistics',data.summary,'points',data.points);
}
const playback=await evaluate(`(async () => {
 const dispatch=e=>testCallSocket.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(e)}));
 const chunk=(id,length,replace)=>dispatch({type:'response.output_audio.delta',generation_id:id,response_id:id,provisional:true,replace_previous:replace,delta:btoa(String.fromCharCode(...new Uint8Array(length*2)))});
 const slider=document.getElementById('call-playback-delay');
 const setDelay=ms=>{slider.value=ms;slider.dispatchEvent(new Event('input',{bubbles:true}));};
 setDelay(725);
 const gap=Number(slider.value);
 if(localStorage.getItem('call_playback_delay_ms')!=='725' || document.getElementById('call-playback-delay-value').value!=='725 ms' || slider.max!=='1000' || slider.step!=='25') throw new Error('Slider preference, range or label not updated');
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 dispatch({type:'input_audio_buffer.speech_started'});
 audioActions=[];
 chunk('test-old',3600,true);chunk('test-new',4800,true);
 await wait(Math.max(10,gap-100));
 if(audioActions.some(e=>e.action==='start')) throw new Error('Played before client silence gap');
 await wait(180);
 if(audioActions.filter(e=>e.action==='start').map(e=>e.length).join()!=='4800') throw new Error('Replaced audio was played or replacement missing');
 chunk('test-third',3000,true);chunk('test-third',1200,false);
 if(!audioActions.some(e=>e.action==='stop'&&e.length===4800)) throw new Error('Replacement did not stop previous audio');
 if(audioActions.some(e=>e.action==='stop'&&e.length===3000)) throw new Error('Continuation stopped current audio');
 if(audioActions.filter(e=>e.action==='start').map(e=>e.length).join()!=='4800,3000,1200') throw new Error('Continuation was not queued');
 dispatch({type:'input_audio_buffer.speech_started'});
 chunk('test-third',2400,false);
 await wait(gap+100);
 if(audioActions.some(e=>e.action==='start'&&e.length===2400)) throw new Error('Played stale continuation after interruption');
 setDelay(1000);
 dispatch({type:'input_audio_buffer.speech_started'});
 chunk('test-slider',1800,true);
 await wait(100);
 if(audioActions.some(e=>e.action==='start'&&e.length===1800)) throw new Error('Ignored increased playback delay');
 setDelay(0);
 if(!audioActions.some(e=>e.action==='start'&&e.length===1800)) throw new Error('Lowering delay did not release buffered audio');
 setDelay(725);
 return audioActions;
})()`);
if(playback.exceptionDetails) throw new Error(JSON.stringify(playback.exceptionDetails));
console.log('client playback policy',JSON.stringify(playback.result.value));
console.log('hangup',JSON.stringify(await evaluate(`document.getElementById('call-end').click();JSON.stringify({status:document.getElementById('call-status').textContent,startVisible:!document.getElementById('call-start').hidden,micEnabled:!document.getElementById('mic-btn').disabled,socket:testCallSocket.readyState})`)));
await call('Page.reload');
await new Promise(r=>setTimeout(r,2000));
if(!(await evaluate(`document.getElementById('call-playback-delay').value === '725' && document.getElementById('call-playback-delay-value').value === '725 ms'`)).result.value) throw new Error('Playback delay did not survive reload');
console.log('playback delay persisted across reload');
ws.close();
if (errors.length) throw new Error('Browser JavaScript errors occurred');
