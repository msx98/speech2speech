"""Incremental CustomVoice audio for the installed Qwen3-TTS 12 Hz model.

The public API only returns complete waveforms. A temporary forward hook
observes complete codec frames, decoding windows with the tokenizer's own
25-frame left context. Callers must serialize model access for the hook's
entire lifetime. No vendor code or model methods are replaced.
"""
import numpy as np


class GenerationCancelled(Exception):
    pass


def stream_custom_voice(model, text, speaker, language, instruct, on_audio,
                        cancelled, chunk_frames=8, **generation):
    import torch

    tokenizer = model.model.speech_tokenizer
    hop = tokenizer.get_decode_upsample_rate()
    talker = model.model.talker
    eos = model.model.config.talker_config.codec_eos_token_id
    frames = []
    emitted = 0
    chunk_frames = max(1, int(chunk_frames))

    def check_cancelled():
        if cancelled is not None and cancelled.is_set():
            raise GenerationCancelled()

    def observe(_module, _args, output):
        nonlocal emitted
        check_cancelled()
        codes = output.hidden_states[-1]
        if codes is None:  # Prompt prefill has no generated codec frame.
            return
        if codes.ndim != 2 or codes.shape[0] != 1:
            raise RuntimeError('Incremental CustomVoice requires a single input')
        if int(codes[0, 0]) == eos:
            return
        frames.append(codes[0].detach())
        if len(frames) - emitted < chunk_frames:
            return
        begin = max(0, emitted - 25)
        window = torch.stack(frames[begin:])
        wavs, rate = tokenizer.decode([{'audio_codes': window}])
        samples = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        offset = (emitted - begin) * hop
        audio = samples[offset:].copy()
        check_cancelled()
        if audio.size:
            on_audio(audio, int(rate))
        emitted = len(frames)

    check_cancelled()
    hook = talker.register_forward_hook(observe)
    try:
        wavs, rate = model.generate_custom_voice(
            text=text, speaker=speaker, language=language, instruct=instruct,
            **generation)
        check_cancelled()
        # The public decode is authoritative for the final partial chunk.
        tail = np.asarray(wavs[0], dtype=np.float32).reshape(-1)[emitted * hop:].copy()
        if tail.size:
            on_audio(tail, int(rate))
    finally:
        hook.remove()
