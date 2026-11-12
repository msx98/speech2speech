#!/usr/bin/env python3
"""OpenAI-style audio API: whisper.cpp CLI STT + sherpa-onnx offline TTS."""
import configparser
import gc
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from voice_profiles import profile_reference, supports_profiles, resolve_reference_audio

CONFIG_PATH = os.getenv('MODELS_INI', '/data/models.ini')
config = configparser.ConfigParser(interpolation=None)
if not config.read(CONFIG_PATH):
    raise RuntimeError(f'Cannot read {CONFIG_PATH}')
server = config['server'] if config.has_section('server') else {}
models = {s[6:]: dict(config[s]) for s in config.sections() if s.startswith('model:')}
for mid, m in models.items():
    if m.get('backend') not in ('whisper.cpp', 'sherpa-onnx', 'qwen-asr', 'qwen-tts', 'omnivoice'):
        raise ValueError(f'{mid}: unsupported backend {m.get("backend")}')
    # Old Qwen configuration used kind=; the newer backends use type=.
    m['type'] = m.get('type', m.get('kind', '')).lower()
    if m['type'] not in ('stt', 'tts'):
        raise ValueError(f'{mid}: type/kind must be stt or tts')
    if (m['backend'] == 'qwen-asr' and m['type'] != 'stt') or (m['backend'] in ('qwen-tts', 'omnivoice') and m['type'] != 'tts'):
        raise ValueError(f'{mid}: backend/kind mismatch')

app = FastAPI(title='Speech API (whisper.cpp + sherpa-onnx + Qwen)')
# A single lock for sherpa inference and model swaps; no potentially thread-owned RLock.
tts_lock = threading.Lock()
tts_cache = OrderedDict()
tts_slots = max(1, int(server.get('tts_slots', '1')))
# whisper-cli launches per request; avoids persistent duplicated GPU model memory.
whisper_lock = threading.Semaphore(max(1, int(server.get('stt_concurrency', '1'))))

# Qwen has separate STT and TTS LRU caches. A global lock prevents eviction
# while either model is performing inference. Only one Uvicorn worker is supported.
qwen_lock = threading.Lock()
qwen_active = {'stt': OrderedDict(), 'tts': OrderedDict()}
qwen_parked = {'stt': OrderedDict(), 'tts': OrderedDict()}
qwen_slots = {k: max(1, int(server.get(f'{k}_slots', '1'))) for k in ('stt', 'tts')}
qwen_eviction = server.get('eviction', 'dispose').lower()
if qwen_eviction not in ('cpu', 'dispose'):
    raise ValueError('eviction must be cpu or dispose')


def qwen_dtype(name):
    import torch
    mapping = {'bfloat16': torch.bfloat16, 'bf16': torch.bfloat16,
               'float16': torch.float16, 'fp16': torch.float16,
               'float32': torch.float32, 'fp32': torch.float32}
    return mapping[name.lower()]


def qwen_release():
    import torch
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def qwen_acquire(mid, m):
    # Caller holds qwen_lock until inference completes.
    kind = m['type']
    cache = qwen_active[kind]
    if mid in cache:
        cache.move_to_end(mid)
        return cache[mid]
    while len(cache) >= qwen_slots[kind]:
        old_mid, old = cache.popitem(last=False)
        if qwen_eviction == 'cpu':
            old.model.to('cpu')
            qwen_parked[kind][old_mid] = old
        else:
            if hasattr(old, 'close'):
                old.close()
            del old
        qwen_release()
    if mid in qwen_parked[kind]:
        model = qwen_parked[kind].pop(mid)
        model.model.to(m.get('device', 'cuda:0'))
    elif m['backend'] == 'omnivoice':
        from omnivoice_bridge import OmniVoiceWorker
        model = OmniVoiceWorker(m)
    else:
        args = dict(device_map=m.get('device', 'cuda:0'),
                    dtype=qwen_dtype(m.get('dtype', 'bfloat16')),
                    attn_implementation='sdpa')
        if kind == 'stt':
            from qwen_asr import Qwen3ASRModel
            model = Qwen3ASRModel.from_pretrained(
                m['path'], max_new_tokens=int(m.get('max_new_tokens', '512')), **args)
        else:
            from qwen_tts import Qwen3TTSModel
            model = Qwen3TTSModel.from_pretrained(m['path'], **args)
    cache[mid] = model
    return model


def qwen_synthesize(mid, m, text, voice, speed, language=None, instruct=None,
                    ref_audio=None, ref_text=None):
    mode = m.get('mode', 'custom_voice').lower()
    if ref_audio and mode != 'voice_clone':
        raise HTTPException(400, 'Reference audio requires a voice_clone model backed by '
                            'Qwen3-TTS Base weights; this model uses ' + mode)
    with qwen_lock:
        model = qwen_acquire(mid, m)
        lang = language or m.get('language', 'Auto')
        if mode == 'custom_voice':
            wavs, rate = model.generate_custom_voice(
                text=text, speaker=voice if voice not in ('', '0') else m.get('speaker', 'Ryan'),
                language=lang, instruct=instruct)
        elif mode == 'voice_design':
            if not instruct:
                raise HTTPException(400, 'voice_design requires instruct')
            wavs, rate = model.generate_voice_design(text=text, language=lang, instruct=instruct)
        elif mode == 'voice_clone':
            if not ref_audio:
                raise HTTPException(400, 'voice_clone requires ref_audio')
            reference = resolve_reference_audio(ref_audio)
            wavs, rate = model.generate_voice_clone(
                text=text, language=lang, ref_audio=str(reference), ref_text=ref_text,
                x_vector_only_mode=not bool(ref_text))
        else:
            raise HTTPException(400, f'Unsupported Qwen TTS mode: {mode}')
        samples = np.asarray(wavs[0], dtype=np.float32).reshape(-1).copy()
    if speed != 1.0 and len(samples) > 1:
        # Pitch-changing resampling, matching the previous Qwen server behavior.
        length = max(1, int(len(samples) / speed))
        samples = np.interp(np.linspace(0, len(samples)-1, length),
                            np.arange(len(samples)), samples).astype(np.float32)
    return samples, int(rate)


def qwen_transcribe(mid, m, wav_path, language, token_callback=None):
    with wave.open(wav_path, 'rb') as wf:
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
        channels = wf.getnchannels()
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return qwen_transcribe_samples(mid, m, samples, rate, language, token_callback)


def qwen_transcribe_samples(mid, m, samples, rate, language=None, token_callback=None, token_budget=None, text_callback=None):
    with qwen_lock:
        model = qwen_acquire(mid, m)
        previous_budget = model.max_new_tokens
        if token_budget is not None:
            model.max_new_tokens = min(previous_budget, token_budget)
        try:
            if token_callback is None:
                result = model.transcribe(audio=(samples, rate), language=language or None)[0]
            else:
                from token_timing import observe_tokens
                with observe_tokens(model.model, token_callback, tokenizer=model.processor.tokenizer,
                                    on_text=text_callback):
                    result = model.transcribe(audio=(samples, rate), language=language or None)[0]
        finally:
            model.max_new_tokens = previous_budget
    return result.text, result.language


def transcribe_pcm(mid, pcm, rate, token_callback=None, text_callback=None):
    m = model_settings(mid, 'stt')
    if m['backend'] != 'qwen-asr':
        raise ValueError('Progressive PCM transcription requires qwen-asr')
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
    if rate != 16000:
        # Avoid the vendor's first-use librosa/Numba resampling compilation
        # in the live path; polyphase filtering also handles arbitrary rates.
        import math
        from scipy.signal import resample_poly
        divisor = math.gcd(rate, 16000)
        samples = resample_poly(samples, 16000 // divisor, rate // divisor).astype(np.float32)
    # Short prefixes must not spend a full utterance's token budget on a
    # speculative continuation. The final decode still gets a duration-sized
    # budget, bounded by the model's configured maximum.
    budget = max(32, int(len(samples) / 16000 * 12) + 16)
    return qwen_transcribe_samples(mid, m, samples, 16000, token_callback=token_callback,
                                   token_budget=budget, text_callback=text_callback)[0]


def stream_synthesize(mid, m, text, voice, speed, language=None, instruct=None,
                      ref_audio=None, ref_text=None, *, on_audio, cancelled=None):
    """Push audio as generated; preserve the regular path for other backends."""
    if m['backend'] == 'qwen-tts' and m.get('mode', 'custom_voice').lower() == 'custom_voice' and speed == 1.0:
        if ref_audio:
            raise HTTPException(400, 'CustomVoice does not accept reference audio')
        from qwen_stream import stream_custom_voice, GenerationCancelled
        with qwen_lock:
            if cancelled is not None and cancelled.is_set():
                return
            model = qwen_acquire(mid, m)
            try:
                stream_custom_voice(
                    model, text, voice if voice not in ('', '0') else m.get('speaker', 'Ryan'),
                    language or m.get('language', 'Auto'), instruct, on_audio, cancelled,
                    chunk_frames=int(server.get('realtime_tts_chunk_frames', '4')))
            except GenerationCancelled:
                return
    else:
        if cancelled is not None and cancelled.is_set():
            return
        audio, rate = synthesize(mid, m, text, voice, speed, language, instruct, ref_audio, ref_text)
        if cancelled is None or not cancelled.is_set():
            on_audio(audio, rate)



def model_settings(mid, kind):
    if mid not in models:
        raise HTTPException(404, f'Unknown model: {mid}')
    m = models[mid]
    if m['type'] != kind:
        raise HTTPException(400, f'{mid} is not a {kind} model')
    return m


def optpath(m, key):
    return m.get(key, '').strip()


def tts_load(mid, m):
    import sherpa_onnx
    family = m.get('family', 'kokoro').lower()
    path = Path(m['path'])
    def file(key, default):
        v = m.get(key, default)
        return str(path / v) if v else ''
    if family == 'kokoro':
        opts = {'kokoro': sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=file('model', 'model.onnx'),
            voices=file('voices', 'voices.bin'),
            tokens=file('tokens', 'tokens.txt'),
            lexicon=file('lexicon', 'lexicon.txt') if m.get('lexicon', 'lexicon.txt') else '',
            data_dir=file('data_dir', 'espeak-ng-data') if m.get('data_dir', 'espeak-ng-data') else '',
        )}
    elif family in ('piper', 'vits'):
        opts = {'vits': sherpa_onnx.OfflineTtsVitsModelConfig(
            model=file('model', 'model.onnx'),
            tokens=file('tokens', 'tokens.txt'),
            lexicon=file('lexicon', 'lexicon.txt') if m.get('lexicon', 'lexicon.txt') else '',
            data_dir=file('data_dir', 'espeak-ng-data') if m.get('data_dir', 'espeak-ng-data') else '',
        )}
    else:
        raise HTTPException(400, f'Unsupported sherpa-onnx TTS family: {family}')
    cfg = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        **opts,
        provider=m.get('provider', 'cpu'),
        num_threads=int(m.get('num_threads', '2')),
    ), max_num_sentences=int(m.get('max_num_sentences', '1')))
    if not cfg.validate():
        raise HTTPException(400, f'Invalid sherpa-onnx config for {mid}; check model files')
    return sherpa_onnx.OfflineTts(cfg)


def synthesize(mid, m, text, voice, speed, language=None, instruct=None, ref_audio=None, ref_text=None):
    if m['backend'] == 'omnivoice':
        if ref_audio:
            reference = resolve_reference_audio(ref_audio)
            ref_audio = str(reference)
        with qwen_lock:
            model = qwen_acquire(mid, m)
            return model.generate(text=text, ref_audio=ref_audio, ref_text=ref_text,
                                  language=None if language in (None, '', 'Auto') else language,
                                  instruct=instruct, speed=speed)
    if m['backend'] == 'qwen-tts':
        return qwen_synthesize(mid, m, text, voice, speed, language, instruct, ref_audio, ref_text)
    import sherpa_onnx
    with tts_lock:
        if mid not in tts_cache:
            while len(tts_cache) >= tts_slots:
                tts_cache.popitem(last=False)
                gc.collect()
            tts_cache[mid] = tts_load(mid, m)
        tts_cache.move_to_end(mid)
        tts = tts_cache[mid]
        voices = [v.strip() for v in m.get('voice_ids', '').split(',') if v.strip()]
        if voices and voice not in voices:
            raise HTTPException(400, f'Unknown voice {voice}; valid voices: {voices}')
        sid = voices.index(voice) if voices else (int(voice) if str(voice).isdigit() else int(m.get('sid', '0')))
        g = sherpa_onnx.GenerationConfig()
        g.sid = sid
        g.speed = speed
        g.silence_scale = float(m.get('silence_scale', '0.2'))
        audio = tts.generate(text, g)
        return np.asarray(audio.samples, dtype=np.float32).copy(), int(audio.sample_rate)


def pcm16(samples):
    return (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()


def wav_bytes(samples, rate):
    mem = io.BytesIO()
    with wave.open(mem, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16(samples))
    return mem.getvalue()


def sentences(text, max_chars):
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    buf = ''
    for part in parts:
        for i in range(0, len(part), max_chars):
            piece = part[i:i + max_chars]
            if buf and len(buf) + len(piece) + 1 > max_chars:
                yield buf
                buf = ''
            buf = (buf + ' ' + piece).strip()
    if buf:
        yield buf


class SpeechRequest(BaseModel):
    model: str = ''
    input: str = Field(min_length=1)
    voice: str = '0'
    response_format: str = 'wav'
    speed: float = Field(default=1.0, gt=0.1, le=4.0)
    stream: bool = False
    language: str | None = None
    instruct: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/v1/models')
def list_models():
    return {'object': 'list', 'data': [
        {'id': mid, 'object': 'model', 'created': 0, 'owned_by': m['backend']}
        for mid, m in models.items()
    ]}


@app.post('/v1/audio/speech')
def speech(req: SpeechRequest):
    req.model = req.model or server.get('default_tts', '')
    m = model_settings(req.model, 'tts')
    if req.voice.startswith('profile:'):
        if req.ref_audio is not None or req.ref_text is not None:
            raise HTTPException(400, 'Use either a named profile or explicit reference audio/text')
        if not supports_profiles(m):
            raise HTTPException(400, 'Named profiles require a cloning model: '
                                'qwen3-tts-1.7b-base or omnivoice')
        reference = profile_reference(req.voice[len('profile:'):])
        req.ref_audio = reference['ref_audio']
        req.ref_text = reference['ref_text']
        req.voice = '0'
    if req.response_format not in ('wav', 'pcm'):
        raise HTTPException(400, 'Supported response_format values: wav, pcm')
    if req.stream and req.response_format != 'pcm':
        raise HTTPException(400, 'Streaming requires response_format=pcm (16-bit signed LE)')
    if not req.stream:
        audio, rate = synthesize(req.model, m, req.input, req.voice, req.speed, req.language, req.instruct, req.ref_audio, req.ref_text)
        if req.response_format == 'pcm':
            return Response(pcm16(audio), media_type='application/octet-stream', headers={'X-Sample-Rate': str(rate)})
        return Response(wav_bytes(audio, rate), media_type='audio/wav')

    # HTTP chunked PCM. Each sentence is synthesized before its samples are emitted.
    # Sample rate must be known before streaming; explicit per-model value required.
    rate = int(m.get('sample_rate', '0'))
    if rate <= 0:
        raise HTTPException(400, 'Set sample_rate in models.ini for PCM streaming')
    chunks = list(sentences(req.input, int(server.get('tts_chunk_chars', '180'))))
    def generate():
        for chunk in chunks:
            audio, actual = synthesize(req.model, m, chunk, req.voice, req.speed, req.language, req.instruct, req.ref_audio, req.ref_text)
            if actual != rate:
                raise RuntimeError(f'Configured sample_rate={rate}, model produced {actual}')
            yield pcm16(audio)
    return StreamingResponse(generate(), media_type='application/octet-stream',
                             headers={'X-Sample-Rate': str(rate), 'X-PCM-Format': 's16le'})


def transcribe_impl(mid, upload, language, prompt, translate, response_format, *, token_callback=None):
    mid = mid or server.get('default_stt', '')
    m = model_settings(mid, 'stt')
    if response_format not in ('json', 'text', 'verbose_json'):
        raise HTTPException(400, 'Supported response_format values: json, text, verbose_json')
    is_qwen = m['backend'] == 'qwen-asr'
    if translate and is_qwen:
        raise HTTPException(501, 'Translation is not supported by this Qwen ASR adapter')
    binary = m.get('binary', server.get('whisper_bin', '/opt/whisper/bin/whisper-cli'))
    if not is_qwen and not shutil.which(binary) and not Path(binary).is_file():
        raise HTTPException(503, f'whisper-cli unavailable: {binary}')
    if not is_qwen and not Path(m['path']).is_file():
        raise HTTPException(503, f'Whisper model missing: {m["path"]}')
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, 'input')
        with open(source, 'wb') as dst:
            shutil.copyfileobj(upload.file, dst)
        audio = os.path.join(tmp, 'audio.wav')
        # Fixed audio format accepted by whisper.cpp.
        convert = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', source, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', audio], capture_output=True, text=True)
        if convert.returncode:
            raise HTTPException(400, f'Audio conversion failed: {convert.stderr[-1200:]}')
        if is_qwen:
            spoken, detected = qwen_transcribe(mid, m, audio, language, token_callback=token_callback)
            if response_format == 'text':
                return Response(spoken, media_type='text/plain; charset=utf-8')
            if response_format == 'verbose_json':
                return {'task': 'transcribe', 'text': spoken, 'language': detected}
            return {'text': spoken}
        outfile = os.path.join(tmp, 'result')
        cmd = [binary, '-m', m['path'], '-f', audio, '-l', language or 'auto', '-oj', '-of', outfile, '-np']
        if translate:
            cmd += ['-tr']
        if prompt:
            cmd += ['--prompt', prompt]
        if m.get('device', '').lower() == 'cpu':
            cmd += ['-ng']
        cmd += ['-t', str(int(m.get('threads', '4')))]
        with whisper_lock:
            done = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=int(server.get('stt_timeout', '600')))
        if done.returncode:
            raise HTTPException(500, f'whisper.cpp failed: {done.stderr[-1500:]}')
        result = json.loads(Path(outfile + '.json').read_text())
        segments = result.get('transcription', [])
        spoken = ''.join(x.get('text', '') for x in segments).strip()
        if response_format == 'text':
            return Response(spoken, media_type='text/plain; charset=utf-8')
        if response_format == 'verbose_json':
            return JSONResponse({'task': 'translate' if translate else 'transcribe', 'text': spoken,
                'language': (result.get('result') or {}).get('language', language or 'unknown'),
                'segments': segments})
        return {'text': spoken}


@app.post('/v1/audio/transcriptions')
def transcriptions(file: UploadFile = File(...), model: str = Form(''), language: str = Form(''),
                   prompt: str = Form(''), response_format: str = Form('json')):
    return transcribe_impl(model, file, language, prompt, False, response_format)


@app.post('/v1/audio/translations')
def translations(file: UploadFile = File(...), model: str = Form(''),
                 prompt: str = Form(''), response_format: str = Form('json')):
    return transcribe_impl(model, file, '', prompt, True, response_format)

from realtime_extension import install as install_realtime
install_realtime(app, server, models, model_settings, transcribe_impl, synthesize, sentences,
                 stream_synthesize=stream_synthesize, transcribe_pcm=transcribe_pcm)
from omnivoice_bridge import close_workers
app.router.on_shutdown.append(close_workers)
from studio import install as install_studio
install_studio(app, speech, SpeechRequest, transcribe_impl, server, models=models)
