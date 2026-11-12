"""Studio chat, conversations and image jobs installed on the speech server.

Speech callbacks use the host's inference functions and model locks. Only LLM
and image generation requests leave the process; no Studio self-proxy exists.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import struct
import uuid
import zlib

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

log = logging.getLogger('studio.chat')


def install(app, tts, transcribe, settings):
    LLM_SYS_MSG = os.getenv('LLM_SYS', settings.get('studio_system_prompt',
                            'You are a helpful, concise voice assistant.'))
    SESSIONS_DIR = os.path.expanduser(os.getenv('S2S_SESSIONS_DIR', '~/.s2s_sessions'))
    IMG_API_BASE = os.getenv('IMG_API_BASE', 'http://127.0.0.1:8080').rstrip('/')
    SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$')

    def stt(data):
        return transcribe(data).get('text', '').strip()

    async def stream_llm(messages, selected_model=""):
        base = settings.get('studio_llm_base_url',
                            settings.get('realtime_llm_base_url', 'http://llama:8080/v1')).rstrip('/')
        model = selected_model or settings.get('studio_llm_model', settings.get('realtime_llm_model', ''))
        key = settings.get('studio_llm_api_key', settings.get('realtime_llm_api_key', ''))
        headers = {'Accept': 'text/event-stream'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        timeout = float(settings.get('realtime_llm_timeout', '180'))
        async with httpx.AsyncClient(timeout=timeout) as client:
            if not model:
                listing = await client.get(base + '/models', headers=headers)
                listing.raise_for_status()
                available = listing.json().get('data', [])
                if not available:
                    raise ValueError('No LLM models found; configure studio_llm_model')
                model = available[0]['id']
            async with client.stream('POST', base + '/chat/completions', headers=headers,
                    json={'model': model, 'messages': messages, 'stream': True,
                          'max_tokens': int(settings.get('realtime_max_tokens', '512'))}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    payload = line[5:].strip()
                    if payload == '[DONE]':
                        return
                    if not payload:
                        continue
                    frame = json.loads(payload)
                    if frame.get('error'):
                        raise RuntimeError(str(frame['error']))
                    for choice in frame.get('choices', []):
                        token = (choice.get('delta') or {}).get('content')
                        if token:
                            yield token

    @app.get('/studio/llm/models')
    async def llm_models():
        base = settings.get('studio_llm_base_url', settings.get('realtime_llm_base_url', 'http://llama:8080/v1')).rstrip('/')
        key = settings.get('studio_llm_api_key', settings.get('realtime_llm_api_key', ''))
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(base + '/models', headers={'Authorization': 'Bearer ' + key} if key else {})
            response.raise_for_status()
            return response.json()

    _msg_keys: dict[tuple[str, str], bytes] = {}  # (conv_id, msg_id) -> session key (outlives WS)
    _conv_locks: dict[str, asyncio.Lock] = {}  # per-conversation write lock

    def _sessions_index_path() -> str:
        return os.path.join(SESSIONS_DIR, "index.json")

    def _conv_path(conv_id: str) -> str:
        # Conversation IDs are also filenames; reject paths received over the socket.
        try:
            uuid.UUID(conv_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Invalid conversation ID')
        return os.path.join(SESSIONS_DIR, f"{conv_id}.jsonl")

    def _init_sessions_dir():
        os.makedirs(SESSIONS_DIR, exist_ok=True)

    def _read_index() -> list[dict]:
        p = _sessions_index_path()
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return json.load(f)

    def _write_index(data: list[dict]):
        with open(_sessions_index_path(), "w") as f:
            json.dump(data, f)

    def _encrypt_msg(key: bytes, conv_id: str, msg_id: str, plaintext: str) -> str:
        """AES-256-GCM encrypt. Returns base64 ciphertext."""
        aesgcm = AESGCM(key)
        digest = hashlib.sha256(f"{conv_id}:{msg_id}".encode()).digest()[:12]
        aad = f"{conv_id}:{msg_id}".encode()
        ct = aesgcm.encrypt(digest, plaintext.encode(), aad)
        return base64.b64encode(ct).decode()

    def _decrypt_msg(key: bytes, conv_id: str, msg_id: str, ciphertext_b64: str) -> str:
        """Decrypt. Returns plaintext string."""
        aesgcm = AESGCM(key)
        digest = hashlib.sha256(f"{conv_id}:{msg_id}".encode()).digest()[:12]
        aad = f"{conv_id}:{msg_id}".encode()
        ct = base64.b64decode(ciphertext_b64)
        return aesgcm.decrypt(digest, ct, aad).decode()

    async def _save_conversation(conv_id: str, msgs: list[dict]):
        """Append messages to the conversation log. Each call appends one msg.
        No read-modify-write — appends are atomic on most filesystems."""
        _init_sessions_dir()
        path = _conv_path(conv_id)
        lines = [json.dumps(m) + "\n" for m in msgs]
        with open(path, "a") as f:
            f.writelines(lines)

    async def _load_conversation(conv_id: str) -> list[dict]:
        """Read all messages from the JSONL log. Each line is one msg."""
        path = _conv_path(conv_id)
        if not os.path.exists(path):
            return []
        msgs = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    msgs.append(json.loads(line))
        return msgs

    async def _modify_conversation(conv_id: str, modifier_fn):
        """Rare: read-all, modify, write-all. Locked for safety (only used by patch)."""
        lock = _conv_locks.setdefault(conv_id, asyncio.Lock())
        async with lock:
            msgs = await _load_conversation(conv_id)
            modifier_fn(msgs)
            _init_sessions_dir()
            path = _conv_path(conv_id)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                for m in msgs:
                    f.write(json.dumps(m) + "\n")
            os.replace(tmp, path)

    async def _update_conv_index(conv_id: str, name: str, msg_count: int):
        _init_sessions_dir()
        idx = _read_index()
        # Remove existing entry for this conv if present
        idx = [c for c in idx if c["id"] != conv_id]
        idx.append({"id": conv_id, "name": name, "created": int(__import__('time').time()),
                    "msg_count": msg_count})
        _write_index(idx)


    async def run_pipeline(ws: WebSocket, transcript: str, history: list, profile: str = "",
                           output_image_token = None, ref_tokens = None,
                           key = None, conv_id = None,
                           session_uid = None, tts_model = '', llm_model = ''):
        """
        1. Append user turn to history (multimodal if image data present).
        2. Stream LLM tokens to client (llm_token frames).
        3. At each sentence boundary run TTS in executor, send tts_sentence frame.
        4. Flush remaining buffer.
        5. Append assistant turn, send audio_end.
        6. If key + conv_id provided, encrypt and save messages to disk.
        """
        pipeline_id  = str(uuid.uuid4())
        loop = asyncio.get_event_loop()

        # Build user message — multimodal if image tokens present
        if transcript or output_image_token or ref_tokens:
            if output_image_token or ref_tokens:
                content: list[dict] = [{"type": "text", "text": transcript or ""}]
                # Reference images
                if ref_tokens:
                    for rt in ref_tokens:
                        b64 = ref_images.get(rt)
                        if b64:
                            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                # Generated output image — check ref_images first (fresh upload),
                # then fall back to img_jobs (original proxy token)
                if output_image_token:
                    b64 = ref_images.get(output_image_token)
                    if not b64:
                        job = img_jobs.get(output_image_token)
                        if job:
                            b64 = job.get("result_b64")
                    if b64:
                        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                history.append({"role": "user", "content": content})
            else:
                history.append({"role": "user", "content": transcript})

        # Save user message (skip if empty — regen/trigger doesn't add a message)
        user_msg_id = None
        if key and conv_id and transcript.strip():
            user_msg_id = str(uuid.uuid4())
            plain = transcript or ""
            enc = _encrypt_msg(key, conv_id, user_msg_id, plain)
            await _save_conversation(conv_id, [{"id": user_msg_id, "role": "user", "encrypted_b64": enc,
                                                "session_uid": session_uid or ""}])
        buf          = ""
        full_reply   = ""
        sentence_id  = 0

        async def flush_sentence(sentence: str):
            nonlocal sentence_id
            sentence = sentence.strip()
            if not sentence:
                return
            try:
                wav  = await loop.run_in_executor(None, tts, sentence, profile, tts_model)
                b64  = base64.b64encode(wav).decode()
                await ws.send_json({
                    "type":        "tts_sentence",
                    "sentence_id": sentence_id,
                    "text":        sentence,
                    "data":        b64,
                })
                sentence_id += 1
            except Exception as e:
                log.warning(f"TTS failed for '{sentence[:40]}': {e}")
                await ws.send_json({'type': 'error', 'message': f'TTS error: {e}',
                                    'pipeline_id': pipeline_id})

        try:
            async for token in stream_llm(history, llm_model):
                buf        += token
                full_reply += token
                await ws.send_json({"type": "llm_token", "text": token, "pipeline_id": pipeline_id})

                # Split on sentence boundaries, keep the incomplete tail
                parts = SENTENCE_RE.split(buf)
                while len(parts) > 1:
                    await flush_sentence(parts.pop(0))
                    buf = " ".join(parts)
                    parts = SENTENCE_RE.split(buf)

            # Flush whatever is left after the stream ends
            if buf.strip():
                await flush_sentence(buf)

        except asyncio.CancelledError:
            # Aborted by user — no error frames, no save
            return
        except Exception as e:
            log.error(f"LLM pipeline error: {e}")
            await ws.send_json({"type": "error", "message": f"LLM error: {e}", "pipeline_id": pipeline_id})

        history.append({"role": "assistant", "content": full_reply})

        # Save assistant reply (append-only, lock-free)
        if key and conv_id and user_msg_id:
            reply_msg_id = str(uuid.uuid4())
            enc = _encrypt_msg(key, conv_id, reply_msg_id, full_reply)
            await _save_conversation(conv_id, [{"id": reply_msg_id, "role": "assistant", "encrypted_b64": enc,
                                                "session_uid": session_uid or ""}])
            # Notify client of new msg count (approximate)
            await ws.send_json({"type": "conv_update", "conv_id": conv_id, "msg_count": "updated"})

        await ws.send_json({"type": "audio_end", "text": full_reply, "pipeline_id": pipeline_id})



    img_jobs: dict[str, dict] = {}  # token -> {status, result_b64, error}

    # Per-WebSocket active pipeline task tracking for abort
    _active_pipeline_tasks: dict[int, asyncio.Task] = {}  # id(ws) -> Task

    def _cancel_and_track_pipeline(ws: WebSocket, task: asyncio.Task) -> None:
        """Cancel any previous pipeline for this WebSocket, track the new one."""
        ws_id = id(ws)
        prev = _active_pipeline_tasks.get(ws_id)
        if prev and not prev.done():
            prev.cancel()
        _active_pipeline_tasks[ws_id] = task
        # Only remove this entry if it still points to THIS task (race-safe: a newer
        # task's callback might have been scheduled before we stored ours)
        task.add_done_callback(lambda _: _active_pipeline_tasks.pop(ws_id, None) if _active_pipeline_tasks.get(ws_id) is task else None)

    background_tasks: set[asyncio.Task] = set()

    def spawn(coro):
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def cleanup():
        tasks = list(background_tasks) + list(_active_pipeline_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.router.on_shutdown.append(cleanup)
    IMG_PROXY_TIMEOUT = 300.0


    async def _fetch_image_proxy(url: str, payload = None, form_data = None):
        """Forward a request to the MFLUX image API and return the image data dict."""
        async with httpx.AsyncClient(timeout=IMG_PROXY_TIMEOUT) as client:
            if form_data:
                # Image-to-image: multipart form
                resp = await client.post(url, data=form_data["data"], files=form_data["files"])
            else:
                resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()


    def _embed_png_metadata(b64: str, meta: dict) -> str:
        """Embed gen params as PNG tEXt chunk. Returns new b64."""
        try:
            raw = base64.b64decode(b64)
            meta_json = json.dumps(meta, default=str).encode("utf-8")
            kw = b"gen_params"
            chunk_data = kw + b"\x00" + meta_json
            chunk_len = struct.pack(">I", len(chunk_data))
            crc = struct.pack(">I", zlib.crc32(b"tEXt" + chunk_data) & 0xFFFFFFFF)
            # Insert right before IEND
            iend = raw.rfind(b"IEND")
            if iend > 0:
                raw = raw[:iend - 4] + chunk_len + b"tEXt" + chunk_data + crc + raw[iend - 4:]
                return base64.b64encode(raw).decode()
        except Exception:
            pass
        return b64


    def _read_png_metadata(raw: bytes):
        """Read gen_params from PNG tEXt chunk. Returns dict or None."""
        try:
            pos = 8  # skip PNG signature
            while pos + 8 < len(raw):
                clen = struct.unpack(">I", raw[pos:pos + 4])[0]
                ctype = raw[pos + 4:pos + 8]
                cdata = raw[pos + 8:pos + 8 + clen]
                if ctype == b"tEXt" and b"gen_params" in cdata:
                    null = cdata.index(b"\x00")
                    return json.loads(cdata[null + 1:null + clen].decode("utf-8"))
                pos += 12 + clen
                if ctype == b"IEND":
                    break
        except Exception:
            pass
        return None


    @app.post("/api/images/generations")
    async def api_image_generate(req: dict):
        """Submit a text-to-image generation job. Returns {token}."""
        token = str(uuid.uuid4())
        img_jobs[token] = {"status": "pending", "result_b64": None, "error": None}

        async def _run():
            try:
                img_jobs[token]["status"] = "processing"
                result = await _fetch_image_proxy(f"{IMG_API_BASE}/v1/images/generations", payload=req)
                data = result.get("data", [{}])[0]
                if data.get("b64_json"):
                    b64 = data["b64_json"]
                elif data.get("url"):
                    async with httpx.AsyncClient() as c:
                        r = await c.get(data["url"])
                        r.raise_for_status()
                        b64 = base64.b64encode(r.content).decode()
                else:
                    raise ValueError('Image backend returned no image')
                # Embed gen params as PNG metadata
                meta = {k: v for k, v in req.items() if k != "assets"}
                b64 = _embed_png_metadata(b64, meta)
                img_jobs[token]["result_b64"] = b64
                img_jobs[token]["gen_params"] = json.dumps(meta, default=str)
                img_jobs[token]["status"] = "done"
                log.info(f"Image gen job {token[:8]} done (t2i)")
            except Exception as e:
                img_jobs[token]["status"] = "error"
                img_jobs[token]["error"] = str(e)
                log.warning(f"Image gen job {token[:8]} failed: {e}")

        spawn(_run())
        return {"token": token}


    @app.post("/api/images/edits")
    async def api_image_edit(
        prompt: str = Form(...),
        model: str = Form(...),
        width: int = Form(...),
        height: int = Form(...),
        strength: float = Form(default=0.75),
        steps: int = Form(default=None),
        guidance: float = Form(default=None),
        seed: int = Form(default=None),
        negative_prompt: str = Form(default=""),
        loras: str = Form(default=""),
        image: list[UploadFile] = Form(default=[]),
    ):
        """Submit an image-to-image (edit) generation job. Returns {token}."""
        # Read file contents NOW while request is still alive
        uploaded: list[tuple[str, bytes, str]] = []
        for f in image:
            contents = await f.read()
            uploaded.append((f.filename or "ref.png", contents, f.content_type or "image/png"))

        token = str(uuid.uuid4())
        img_jobs[token] = {"status": "pending", "result_b64": None, "error": None}

        async def _run():
            try:
                img_jobs[token]["status"] = "processing"
                files = [("image", (name, data, mime)) for name, data, mime in uploaded]

                form_data = {
                    "data": {
                        "prompt": prompt,
                        "model": model,
                        "width": str(width),
                        "height": str(height),
                        "strength": str(strength),
                    },
                    "files": files,
                }
                if loras:
                    form_data["data"]["loras"] = loras
                if steps is not None:
                    form_data["data"]["steps"] = str(steps)
                if guidance is not None:
                    form_data["data"]["guidance"] = str(guidance)
                if seed is not None:
                    form_data["data"]["seed"] = str(seed)
                if negative_prompt:
                    form_data["data"]["negative_prompt"] = negative_prompt

                result = await _fetch_image_proxy(f"{IMG_API_BASE}/v1/images/edits", form_data=form_data)
                data = result.get("data", [{}])[0]
                b64 = None
                if data.get("b64_json"):
                    b64 = data["b64_json"]
                elif data.get("url"):
                    async with httpx.AsyncClient() as c:
                        r = await c.get(data["url"])
                        r.raise_for_status()
                        b64 = base64.b64encode(r.content).decode()
                if b64:
                    # Embed gen params as PNG metadata (mirrors T2I endpoint)
                    meta = {"prompt": prompt, "model": model, "width": width, "height": height}
                    if negative_prompt:
                        meta["negative_prompt"] = negative_prompt
                    if steps is not None:
                        meta["steps"] = steps
                    if guidance is not None:
                        meta["guidance"] = guidance
                    if seed is not None:
                        meta["seed"] = seed
                    b64 = _embed_png_metadata(b64, meta)
                    img_jobs[token]["result_b64"] = b64
                    img_jobs[token]["gen_params"] = json.dumps(meta, default=str)
                img_jobs[token]["status"] = "done"
                log.info(f"Image edit job {token[:8]} done (i2i)")
            except Exception as e:
                img_jobs[token]["status"] = "error"
                img_jobs[token]["error"] = str(e)
                log.warning(f"Image edit job {token[:8]} failed: {e}")

        spawn(_run())
        return {"token": token}


    @app.get("/api/images/status/{token}")
    async def api_image_status(token: str):
        job = img_jobs.get(token)
        if not job:
            raise HTTPException(404, "Job not found")
        resp = {"status": job["status"]}
        if job["status"] == "done":
            resp["b64_json"] = job["result_b64"]
            gp = job.get("gen_params")
            if gp:
                resp["gen_params"] = gp
        if job["status"] == "error":
            resp["error"] = job["error"]
        return resp


    # Store reference images uploaded from the browser for LLM context
    ref_images: dict[str, str] = {}  # token -> b64_json


    @app.post("/api/images/upload-ref")
    async def api_upload_ref(image: UploadFile = File(...)):
        """Upload a reference image. Returns {token} and optional gen_params."""
        contents = await image.read()
        b64 = base64.b64encode(contents).decode()
        token = str(uuid.uuid4())
        ref_images[token] = b64
        resp = {"token": token}
        gp = _read_png_metadata(contents)
        if gp:
            resp["gen_params"] = gp
        return resp


    @app.get("/api/images/ref/{token}")
    async def api_get_ref(token: str):
        """Get a reference image by token (returns JSON with b64_json)."""
        b64 = ref_images.get(token)
        if not b64:
            raise HTTPException(404, "Image not found")
        return {"b64_json": b64}


    @app.get("/api/images/models")
    async def api_image_models():
        """Proxy: list available image generation models from the MFLUX API."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{IMG_API_BASE}/v1/models")
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log.warning(f"Failed to fetch models from image API: {e}")
            # Return a sensible fallback
            return {
                "data": [
                    {"id": "flux2-klein-4b"},
                    {"id": "flux2-klein-9b"},
                    {"id": "z-image-turbo"},
                    {"id": "qwen-image-edit"},
                    {"id": "flux2-klein-edit-4b"},
                    {"id": "flux2-klein-edit-9b"},
                    {"id": "flux2-klein-edit-9b-kv"},
                    {"id": "z-image-turbo-edit"},
                ]
            }


    @app.get("/api/images/loras")
    async def api_image_loras():
        """List available LoRA files in the ComfyUI loras directory."""
        lora_dir = os.getenv("IMG_LORA_DIR", "/home/models/loras")
        try:
            files = []
            for f in os.listdir(lora_dir):
                if f.endswith((".safetensors", ".bin", ".pt")):
                    files.append({"name": f, "path": os.path.join(lora_dir, f)})
            files.sort(key=lambda x: x["name"].lower())
            return {"loras": files}
        except Exception as e:
            log.warning(f"Failed to list LoRA files: {e}")
            return {"loras": []}


    # ---------------------------------------------------------------------------
    # WebSocket — streaming speech-to-speech
    # ---------------------------------------------------------------------------

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        log.info("Client connected")

        # Per-connection state
        history: list = [{"role": "system", "content": LLM_SYS_MSG}]
        audio_chunks: list[bytes] = []
        ws_key = None     # 32-byte session key = key XOR nonce (from client)
        active_conv_id = None
        active_session_uid = None  # ties messages to a client session

        await websocket.send_json({
            "type":    "ready",
            "message": "Connected — click the mic or type to start.",
        })

        loop = asyncio.get_event_loop()

        try:
            while True:
                raw   = await websocket.receive_text()
                frame = json.loads(raw)
                ftype = frame.get("type")

                # ── set_key: client sends (session_uid, session_key = key XOR nonce) ──
                if ftype == "set_key":
                    session_key_b64 = frame.get("session_key", "")
                    suid = frame.get("session_uid", "")
                    try:
                        ws_key = base64.b64decode(session_key_b64)
                        if len(ws_key) != 32:
                            ws_key = None
                            raise ValueError("session_key must be 32 bytes")
                        active_session_uid = suid
                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": f"Invalid session key: {e}"})
                        continue
                    log.info(f"Session {suid[:12]} established key")
                    # Send conversation list
                    idx = _read_index()
                    await websocket.send_json({"type": "conv_list", "conversations": idx})

                # ── audio_start: new recording session ─────────────────────────
                elif ftype == "audio_start":
                    audio_chunks.clear()

                # ── audio_chunk: accumulate encoded audio ──────────────────────
                elif ftype == "audio_chunk":
                    try:
                        audio_chunks.append(base64.b64decode(frame["data"]))
                    except Exception:
                        pass  # malformed chunk — skip

                # ── audio_end: run full pipeline ───────────────────────────────
                elif ftype == "audio_end":
                    if not audio_chunks:
                        await websocket.send_json({"type": "error", "message": "No audio received."})
                        continue

                    raw_audio = b"".join(audio_chunks)
                    audio_chunks.clear()

                    # STT
                    try:
                        transcript = await loop.run_in_executor(None, stt, raw_audio)
                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": f"STT error: {e}"})
                        continue

                    if not transcript:
                        await websocket.send_json({"type": "error", "message": "Could not transcribe — try speaking louder."})
                        continue

                    log.info(f"STT: {transcript[:80]}")
                    await websocket.send_json({"type": "stt_text", "text": transcript})

                    # LLM + TTS in background so the WS loop stays responsive
                    profile = frame.get("profile", "")
                    _cancel_and_track_pipeline(websocket,
                        asyncio.create_task(run_pipeline(websocket, transcript, history, profile,
                          key=ws_key, conv_id=active_conv_id,
                          session_uid=active_session_uid, tts_model=frame.get('tts_model', ''), llm_model=frame.get('llm_model', ''))))

                # ── text_message: typed input, same pipeline as voice ──────────
                elif ftype == "text_message":
                    text    = frame.get("text", "").strip()
                    profile = frame.get("profile", "")
                    output_image_token = frame.get("output_image_token")
                    ref_tokens = frame.get("ref_tokens")
                    # Set active conversation from client
                    conv_from_frame = frame.get("conv_id")
                    if conv_from_frame:
                        active_conv_id = conv_from_frame
                    elif not active_conv_id:
                        active_conv_id = str(uuid.uuid4())
                    # Always run the pipeline — empty text means "continue from existing context"
                    _cancel_and_track_pipeline(websocket,
                        asyncio.create_task(run_pipeline(websocket, text, history, profile,
                          output_image_token, ref_tokens,
                          key=ws_key, conv_id=active_conv_id,
                          session_uid=active_session_uid, tts_model=frame.get('tts_model', ''), llm_model=frame.get('llm_model', ''))))

                # ── abort_pipeline: cancel an ongoing generation stream ──
                elif ftype == "abort_pipeline":
                    pid = frame.get("pipeline_id")
                    task = _active_pipeline_tasks.get(id(websocket))
                    if task and not task.done():
                        task.cancel()
                        await websocket.send_json({"type": "audio_end",
                            "text": "", "pipeline_id": pid or "",
                            "aborted": True})

                # ── load_conv: send encrypted messages to client for local decryption ──
                elif ftype == "load_conv":
                    cid = frame.get("conv_id")
                    if not cid:
                        await websocket.send_json({"type": "error", "message": "Missing conv_id"})
                        continue
                    msgs = await _load_conversation(cid)
                    # Send raw encrypted blobs — client decrypts locally with its nonce
                    active_conv_id = cid
                    await websocket.send_json({
                        "type": "conv_data",
                        "conv_id": cid,
                        "messages": [{"role": m["role"], "encrypted_b64": m["encrypted_b64"],
                                      "msg_id": m["id"], "session_uid": m.get("session_uid", "")}
                                     for m in msgs]
                    })

                # ── set_conv_history: client sends back decrypted history for LLM context ──
                elif ftype == "set_conv_history":
                    cid = frame.get("conv_id")
                    msgs = frame.get("messages", [])
                    if cid:
                        active_conv_id = cid
                        history.clear()
                        history.append({"role": "system", "content": LLM_SYS_MSG})
                        for m in msgs:
                            history.append({"role": m["role"], "content": m["content"]})
                        log.info(f"Restored history for {cid[:12]}: {len(msgs)} messages")

                # ── save_img_bubble: persist an image gen bubble as a conversation msg ──
                elif ftype == "save_img_bubble":
                    cid = frame.get("conv_id")
                    params = frame.get("params", {})
                    has_output = frame.get("has_output", False)
                    output_b64 = frame.get("output_b64")      # output image base64 (may be null)
                    assets = frame.get("assets", [])           # [{dataUrl, name, id, refToken}, ...]
                    if not cid or not ws_key:
                        continue
                    msg_id = str(uuid.uuid4())
                    _msg_keys[(cid, msg_id)] = ws_key
                    img_data = {
                        "type": "img", "role": "assistant",
                        "prompt": params.get("prompt", ""),
                        "model": params.get("model", ""),
                        "steps": params.get("steps", 2),
                        "guidance": params.get("guidance", 3.5),
                        "seed": params.get("seed"),
                        "size": params.get("size", "1024x1024"),
                        "negative_prompt": params.get("negative_prompt", ""),
                        "hasOutput": has_output,
                        "outputImageData": output_b64,
                        "assets": assets,
                        "proxyToken": params.get("proxyToken"),
                    }
                    plain = json.dumps(img_data)
                    enc = _encrypt_msg(ws_key, cid, msg_id, plain)
                    await _save_conversation(cid, [{"id": msg_id, "role": "assistant", "encrypted_b64": enc,
                                                    "session_uid": active_session_uid or ""}])
                    await _update_conv_index(cid, cid[:8], 0)  # count is best-effort
                    await websocket.send_json({"type": "img_bubble_saved", "conv_id": cid, "msg_id": msg_id})

                # ── patch_img_bubble: update an existing image bubble ────────────
                elif ftype == "patch_img_bubble":
                    cid = frame.get("conv_id")
                    msg_id = frame.get("msg_id")
                    patch_params = frame.get("params", {})
                    has_output = frame.get("has_output", None)
                    output_b64 = frame.get("output_b64")
                    assets = frame.get("assets")
                    if not cid or not msg_id:
                        continue
                    key = _msg_keys.get((cid, msg_id)) or ws_key
                    if not key:
                        continue
                    found_holder = [False]
                    def _do_patch(msgs):
                        for m in msgs:
                            if m["id"] == msg_id:
                                try:
                                    plain = _decrypt_msg(key, cid, msg_id, m["encrypted_b64"])
                                    data = json.loads(plain)
                                    if isinstance(data, dict) and data.get("type") == "img":
                                        for k in ("prompt", "model", "steps", "guidance", "seed", "size", "negative_prompt", "proxyToken"):
                                            if k in patch_params:
                                                data[k] = patch_params[k]
                                        if has_output is not None:
                                            data["hasOutput"] = has_output
                                        if output_b64 is not None:
                                            data["outputImageData"] = output_b64
                                        if assets is not None:
                                            data["assets"] = assets
                                        m["encrypted_b64"] = _encrypt_msg(key, cid, msg_id, json.dumps(data))
                                        found_holder[0] = True
                                except Exception:
                                    pass
                                break
                    await _modify_conversation(cid, _do_patch)

                # ── edit_text_msg: edit an existing text message ────────────────
                elif ftype == "edit_text_msg":
                    cid = frame.get("conv_id")
                    msg_id = frame.get("msg_id")
                    new_text = frame.get("text", "")
                    if not cid or not msg_id:
                        continue
                    key = _msg_keys.get((cid, msg_id)) or ws_key
                    if not key:
                        continue
                    found_holder = [False]
                    def _do_edit(msgs):
                        for m in msgs:
                            if m["id"] == msg_id:
                                try:
                                    plain = _decrypt_msg(key, cid, msg_id, m["encrypted_b64"])
                                    # Re-encrypt with the new text as a plain string (not JSON)
                                    m["encrypted_b64"] = _encrypt_msg(key, cid, msg_id, new_text)
                                    found_holder[0] = True
                                except Exception:
                                    pass
                                break
                    await _modify_conversation(cid, _do_edit)

                # ── send_as_role: save a message as a specific role (no LLM) ────
                elif ftype == "send_as_role":
                    cid = frame.get("conv_id")
                    role = frame.get("role", "user")
                    msg_text = frame.get("text", "")
                    if not cid or not ws_key:
                        continue
                    msg_id = str(uuid.uuid4())
                    _msg_keys[(cid, msg_id)] = ws_key
                    plain = msg_text
                    enc = _encrypt_msg(ws_key, cid, msg_id, plain)
                    await _save_conversation(cid, [{"id": msg_id, "role": role, "encrypted_b64": enc,
                                                    "session_uid": active_session_uid or ""}])

                # ── new_conv: create a new conversation ────────────────────────
                elif ftype == "new_conv":
                    cid = str(uuid.uuid4())
                    name = frame.get("name", cid[:8])
                    history.clear()
                    history.append({"role": "system", "content": LLM_SYS_MSG})
                    active_conv_id = cid
                    await _update_conv_index(cid, name, 0)
                    await websocket.send_json({"type": "conv_created", "conv_id": cid, "name": name})
                    # Also send updated list
                    idx = _read_index()
                    await websocket.send_json({"type": "conv_list", "conversations": idx})

                # ── delete_conv: delete a conversation ─────────────────────────
                elif ftype == "delete_conv":
                    cid = frame.get("conv_id")
                    if not cid:
                        continue
                    path = _conv_path(cid)
                    if os.path.exists(path):
                        os.unlink(path)
                    idx = _read_index()
                    idx = [c for c in idx if c["id"] != cid]
                    _write_index(idx)
                    if active_conv_id == cid:
                        active_conv_id = None
                        history.clear()
                        history.append({"role": "system", "content": LLM_SYS_MSG})
                    await websocket.send_json({"type": "conv_deleted", "conv_id": cid})
                    idx = _read_index()
                    await websocket.send_json({"type": "conv_list", "conversations": idx})

                # ── rename_conv: rename a conversation ─────────────────────────
                elif ftype == "rename_conv":
                    cid = frame.get("conv_id")
                    name = frame.get("name", "").strip()
                    if not cid or not name:
                        continue
                    idx = _read_index()
                    for c in idx:
                        if c["id"] == cid:
                            c["name"] = name
                            break
                    _write_index(idx)
                    await websocket.send_json({"type": "conv_renamed", "conv_id": cid, "name": name})
                    await websocket.send_json({"type": "conv_list", "conversations": idx})

                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown frame type: {ftype}"})

        except WebSocketDisconnect:
            log.info("Client disconnected")
        except Exception as e:
            log.error(f"WS error: {e}", exc_info=True)
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass


        finally:
            task = _active_pipeline_tasks.pop(id(websocket), None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
