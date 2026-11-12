"""Studio UI and voice tools for the unified speech server.

Install with the host's synchronous speech endpoint, request model and
transcribe_impl. Inference runs in worker threads using the host's model locks.
"""
import asyncio
import base64
import io
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from voice_profiles import profile_reference, supports_profiles, voice_directory

log = logging.getLogger('studio')

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Small helpers (pure)
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return Path(name).name


def _safe_key(name: str) -> str:
    return re.sub(r'[^\w\-]', '_', name.strip()) or 'default'


class SaveTxtRequest(BaseModel):
    name: str
    text: str


class DuplicateClipRequest(BaseModel):
    name: str


class Segment(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    text: str = ''


class CreateSegmentsRequest(BaseModel):
    temp_id: str
    name: str
    segments: list[Segment]


# ---------------------------------------------------------------------------
# install() — register the studio on any FastAPI app (realtime-style)
# ---------------------------------------------------------------------------

def install(app, speech, speech_request, transcribe_impl, settings,
            voice_dir: Optional[str] = None, title: Optional[str] = None, models=None):
    VOICE_DIR = Path(voice_dir).expanduser() if voice_dir else voice_directory()
    page_title = title or os.getenv('S2S_TITLE', 'Voice Studio')

    temps: dict[str, dict] = {}
    jobs: dict[str, dict] = {}
    tasks: set[asyncio.Task] = set()

    def track(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def transcribe(data: bytes, response_format='json'):
        upload = UploadFile(filename='audio', file=io.BytesIO(data))
        try:
            result = transcribe_impl(settings.get('studio_stt_model', ''), upload, '', '', False, response_format)
            if isinstance(result, Response):
                return json.loads(result.body)
            return result
        finally:
            upload.file.close()

    def synthesize_text(text: str, profile: str = '', model: str = '') -> bytes:
        reference = {}
        explicit_model = bool(model)
        model = model or settings.get('studio_tts_model', '') or settings.get('default_tts', '')
        if profile:
            # An explicit model must support the requested profile. Automatic mode
            # can route references to the configured cloning model.
            if not explicit_model:
                clone_model = settings.get('studio_clone_model', '')
                if not clone_model and models is not None:
                    configured = models.get(model, {})
                    if supports_profiles(configured):
                        clone_model = model
                    else:
                        candidates = [mid for mid, config in models.items()
                                      if supports_profiles(config)]
                        if len(candidates) == 1:
                            clone_model = candidates[0]
                if clone_model:
                    model = clone_model
            if models is not None:
                configured = models.get(model, {})
                if not supports_profiles(configured):
                    raise ValueError('This voice actor profile needs a voice-cloning model. '
                                     'Select OmniVoice or a Qwen3-TTS Base model configured with mode=voice_clone, '
                                     'or choose No Reference for a built-in speaker.')
            reference = profile_reference(profile, VOICE_DIR)
        request = speech_request(input=text,
                    model=model,
                    voice=settings.get('studio_voice', '0'), **reference)
        return speech(request).body

    # Register the actual chat socket and image routes, with local speech callbacks.
    from chat_extension import install as install_chat
    install_chat(app, synthesize_text, transcribe, settings)

    async def cleanup():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for entry in temps.values():
            Path(entry['local']).unlink(missing_ok=True)

    app.router.on_shutdown.append(cleanup)

    static_dir = HERE / 'static'
    templates = Jinja2Templates(directory=str(static_dir))

    # Avoid clobbering routes the host app already owns.
    existing = {getattr(r, 'path', None) for r in app.routes}
    static_url = '/static' if '/static' not in existing else '/studio-static'
    health_url = '/health' if '/health' not in existing else '/studio/health'
    index_path = '/' if '/' not in existing else '/studio'
    if static_url not in existing:
        app.mount(static_url, StaticFiles(directory=str(static_dir)), name='studio-static')

    async def _transcribe_file(wav_path: str) -> str:
        return (await asyncio.to_thread(transcribe, Path(wav_path).read_bytes())).get('text', '')

    @app.get('/tts/models')
    async def tts_models():
        return {'default': settings.get('studio_tts_model', '') or settings.get('default_tts', ''),
                'models': [{'id': mid, 'mode': config.get('mode', ''),
                            'supports_profiles': supports_profiles(config)}
                           for mid, config in (models or {}).items()
                           if config.get('type', config.get('kind')) == 'tts']}

    # ---- page + health ------------------------------------------------------

    @app.get(index_path, response_class=HTMLResponse)
    async def studio_index(request: Request):
        return templates.TemplateResponse(request, 'index.jinja', {
            'page_title': page_title,
            'voice_dir': str(VOICE_DIR),
            'static_url': static_url,
            'asset_version': hashlib.sha256(b''.join((static_dir / name).read_bytes() for name in ('studio.js', 'call.js', 'call-stats.js'))).hexdigest()[:12],
            'health_url': health_url,
        })

    @app.get(health_url)
    async def studio_health():
        return {'status': 'ok', 'backend': 'ok',
                'voice_dir': str(VOICE_DIR),
                'voice_dir_writable': os.access(VOICE_DIR, os.W_OK) if VOICE_DIR.exists() else False}

    # ---- voice clip management (stored in VOICE_DIR) ------------------------

    @app.get('/tts/config/list')
    async def config_list():
        if not VOICE_DIR.exists():
            return {'clips': [], 'last_used': []}
        clips = []
        for txt_file in sorted(VOICE_DIR.glob('*.txt')):
            stem = txt_file.stem
            if stem in ('ref', 'concat_list'):
                continue
            wav_file = VOICE_DIR / f'{stem}.wav'
            if wav_file.exists():
                try:
                    txt_content = txt_file.read_text(encoding='utf-8').strip()
                except Exception:
                    txt_content = ''
                clips.append({'name': stem, 'txt': txt_file.name, 'wav': wav_file.name,
                              'txt_content': txt_content})
        last_used = []
        lu = VOICE_DIR / 'last_used.json'
        if lu.exists():
            try:
                last_used = json.loads(lu.read_text(encoding='utf-8'))
            except Exception:
                pass
        return {'clips': clips, 'last_used': last_used}

    @app.post('/tts/config/rebuild')
    async def config_rebuild(names: list[str] = Form(...), profile_name: str = Form(...)):
        safe_profile = _safe_key(profile_name)
        profile_dir = VOICE_DIR / 'profiles' / safe_profile
        profile_dir.mkdir(parents=True, exist_ok=True)

        wav_files, txt_parts = [], []
        for name in names:
            wav_file = VOICE_DIR / f'{_safe(name)}.wav'
            txt_file = VOICE_DIR / f'{_safe(name)}.txt'
            if wav_file.exists() and txt_file.exists():
                wav_files.append(str(wav_file))
                txt_parts.append(txt_file.read_text(encoding='utf-8').strip())
        if not wav_files:
            raise HTTPException(400, 'No valid clips selected')

        ref_txt = profile_dir / 'ref.txt'
        ref_txt.write_text('; '.join(txt_parts), encoding='utf-8')

        ref_wav = profile_dir / 'ref.wav'
        concat_file = profile_dir / 'concat_list.txt'
        try:
            concat_file.write_text(''.join(f"file '{w}'\n" for w in wav_files), encoding='utf-8')
            await asyncio.get_event_loop().run_in_executor(None, lambda: subprocess.run(
                ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                 '-f', 'concat', '-safe', '0', '-i', str(concat_file),
                 '-c', 'copy', str(ref_wav)], check=True))
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f'ffmpeg failed: {e}')
        finally:
            concat_file.unlink(missing_ok=True)

        (VOICE_DIR / 'last_used.json').write_text(json.dumps(names), encoding='utf-8')
        log.info(f'Rebuilt ref.wav for profile {safe_profile!r} from {len(wav_files)} clips')
        return {'status': 'ok', 'clips_used': len(wav_files), 'profile': safe_profile,
                'ref_wav': str(ref_wav), 'ref_txt': str(ref_txt)}

    @app.get('/tts/config/wav/{name}')
    async def config_wav(name: str):
        wav_file = VOICE_DIR / f'{_safe(name)}.wav'
        if not wav_file.exists():
            raise HTTPException(404, 'Clip not found')
        return Response(content=wav_file.read_bytes(), media_type='audio/wav')

    @app.post('/tts/config/save-txt')
    async def config_save_txt(req: SaveTxtRequest):
        txt_file = VOICE_DIR / f'{_safe(req.name)}.txt'
        if not txt_file.exists():
            raise HTTPException(404, 'Clip not found')
        txt_file.write_text(req.text.strip(), encoding='utf-8')
        return {'status': 'ok'}

    @app.post('/tts/config/duplicate-clip')
    async def config_duplicate_clip(req: DuplicateClipRequest):
        name = _safe(req.name)
        src_wav = VOICE_DIR / f'{name}.wav'
        src_txt = VOICE_DIR / f'{name}.txt'
        if not src_wav.exists():
            raise HTTPException(404, 'Clip not found')
        i = 2
        while (VOICE_DIR / f'{name}_{i}.wav').exists():
            i += 1
        new_name = f'{name}_{i}'
        shutil.copy2(src_wav, VOICE_DIR / f'{new_name}.wav')
        txt_content = ''
        if src_txt.exists():
            shutil.copy2(src_txt, VOICE_DIR / f'{new_name}.txt')
            txt_content = src_txt.read_text(encoding='utf-8').strip()
        (VOICE_DIR / f'{new_name}.txt').write_text(txt_content, encoding='utf-8')
        return {'name': new_name, 'txt_content': txt_content}

    @app.post('/tts/config/upload-temp')
    async def upload_temp(file: UploadFile = File(...)):
        """Convert upload to WAV for local cutting and transcription."""
        suffix = Path(file.filename or 'audio').suffix or '.bin'
        fd_raw, raw_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd_raw, 'wb') as f:
                shutil.copyfileobj(file.file, f)
        finally:
            await file.close()

        fd_wav, wav_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd_wav)
        try:
            await asyncio.get_event_loop().run_in_executor(None, lambda: subprocess.run(
                ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', raw_path, wav_path],
                check=True))
        except subprocess.CalledProcessError as e:
            os.unlink(wav_path)
            raise HTTPException(400, f'Cannot decode audio file: {e}')
        finally:
            try:
                os.unlink(raw_path)
            except OSError:
                pass

        temp_id = str(uuid.uuid4())
        temps[temp_id] = {'local': wav_path}
        return {'temp_id': temp_id}

    @app.post('/tts/config/create-segments')
    async def create_segments(req: CreateSegmentsRequest):
        entry = temps.get(req.temp_id)
        if not entry or not os.path.exists(entry['local']):
            raise HTTPException(404, 'Upload not found or expired')

        safe_name = _safe_key(req.name)
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()

        def cut(seg: Segment, out_wav: str):
            cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', entry['local']]
            if seg.start is not None and seg.start > 0:
                cmd += ['-ss', f'{seg.start:.3f}']
            if seg.end is not None:
                cmd += ['-to', f'{seg.end:.3f}']
            cmd.append(out_wav)
            subprocess.run(cmd, check=True)

        created = []
        for i, seg in enumerate(req.segments, 1):
            out_name = f'{safe_name}_{i}'
            out_wav = VOICE_DIR / f'{out_name}.wav'
            try:
                await loop.run_in_executor(None, cut, seg, str(out_wav))
            except subprocess.CalledProcessError as e:
                raise HTTPException(500, f'ffmpeg failed on segment {i}: {e}')
            (VOICE_DIR / f'{out_name}.txt').write_text(seg.text.strip(), encoding='utf-8')
            created.append(out_name)

        try:
            os.unlink(entry['local'])
        except OSError:
            pass
        del temps[req.temp_id]
        return {'created': created, 'count': len(created)}

    @app.post('/tts/config/transcribe-segment')
    async def transcribe_segment_endpoint(req: dict):
        """Slice the local temp upload, then transcribe the slice locally."""
        entry = temps.get(req.get('temp_id', ''))
        if not entry or not os.path.exists(entry['local']):
            raise HTTPException(404, 'Upload not found')
        fd, cut_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', entry['local']]
        start, end = req.get('start'), req.get('end')
        if start is not None and start > 0:
            cmd += ['-ss', f'{start:.3f}']
        if end is not None:
            cmd += ['-to', f'{end:.3f}']
        cmd += ['-ar', '16000', '-ac', '1', cut_path]
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: subprocess.run(cmd, check=True))
            return {'text': await _transcribe_file(cut_path)}
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f'ffmpeg failed: {e}')
        finally:
            try:
                os.unlink(cut_path)
            except OSError:
                pass

    @app.post('/tts/config/auto-segment')
    async def auto_segment_endpoint(req: dict):
        """Use timestamps from the selected STT model, or return one full clip."""
        entry = temps.get(req.get('temp_id', ''))
        if not entry or not os.path.isfile(entry['local']):
            raise HTTPException(404, 'Upload not found')
        result = await asyncio.to_thread(transcribe, Path(entry['local']).read_bytes(), 'verbose_json')
        segments = []
        for segment in result.get('segments', []):
            offsets = segment.get('offsets', {})
            start = segment.get('start', offsets.get('from', 0) / 1000)
            end = segment.get('end', offsets.get('to', 0) / 1000)
            if end > start:
                segments.append({'start': round(start, 2), 'end': round(end, 2),
                                 'text': segment.get('text', '').strip()})
        if not segments and result.get('text', '').strip():
            def duration():
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                    'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
                    entry['local']], capture_output=True, text=True, check=True)
                return float(probe.stdout.strip())
            segments.append({'start': 0, 'end': round(await asyncio.to_thread(duration), 2),
                             'text': result['text'].strip()})
        return {'segments': segments}

    @app.post('/tts/config/transcribe-clip')
    async def config_transcribe_clip(req: dict):
        wav_file = VOICE_DIR / f'{_safe(req.get("name", ""))}.wav'
        if not wav_file.exists():
            raise HTTPException(404, 'Clip WAV not found')
        return {'text': await _transcribe_file(str(wav_file))}

    # ---- TTS jobs and text tools ---------------------------------------------

    @app.post('/transcribe')
    async def transcribe_endpoint(file: UploadFile = File(...)):
        try:
            return await asyncio.to_thread(transcribe, await file.read())
        finally:
            await file.close()

    @app.post('/tts/config/to-romaji')
    async def config_to_romaji(req: dict):
        def convert():
            try:
                import cutlet
            except ImportError:
                raise HTTPException(503, 'Romaji conversion requires cutlet and unidic-lite')
            return cutlet.Cutlet().romaji(req.get('text', ''))
        return {'text': await asyncio.to_thread(convert)}

    @app.post('/tts/submit')
    async def tts_submit(text: str = Form(...), ref: str = Form(''), model: str = Form('')):
        if not text.strip():
            raise HTTPException(400, 'Text is required')
        token = str(uuid.uuid4())
        jobs[token] = {'status': 'pending', 'text': text, 'profile': ref, 'model': model}

        async def run():
            job = jobs[token]
            job['status'] = 'processing'
            try:
                audio = await asyncio.to_thread(synthesize_text, text, ref, model)
                job.update(status='done', audio=base64.b64encode(audio).decode())
            except Exception as exc:
                job.update(status='error', error=str(exc))
        track(run())
        return {'token': token, 'status': 'pending'}

    @app.get('/tts/status/{token}')
    async def tts_status(token: str):
        if token not in jobs:
            raise HTTPException(404, 'Job not found')
        return jobs[token]

    log.info('Studio installed with local speech and chat handlers at %s', index_path)
