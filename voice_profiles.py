"""Resolve Studio's named reference profiles for internal and public API callers."""
import os
from pathlib import Path
import re

from fastapi import HTTPException


def voice_directory():
    configured = os.getenv('S2S_VOICE_DIR')
    if configured:
        return Path(configured).expanduser()
    if Path('/data/voices').is_dir():
        return Path('/data/voices/ANGEL')
    return Path(__file__).resolve().parent.parent / 'data' / 'voices' / 'ANGEL'


def supports_profiles(model):
    return model.get('backend') == 'omnivoice' or (
        model.get('backend') == 'qwen-tts' and model.get('mode', '').lower() == 'voice_clone')


def profile_reference(name, directory=None):
    if not name.strip():
        raise HTTPException(400, 'A profile name is required after profile:')
    key = re.sub(r'[^\w\-]', '_', name.strip())
    root = ((Path(directory) if directory is not None else voice_directory()) / 'profiles').resolve()
    profile = (root / key).resolve()
    if not profile.is_relative_to(root):
        raise HTTPException(400, 'Profile must be stored under the voice profiles directory')
    wav, text = profile / 'ref.wav', profile / 'ref.txt'
    if not wav.is_file():
        raise HTTPException(404, f'Voice profile {name!r} has no reference audio. '
                                'Open Config and choose Regenerate Reference.')
    return {'ref_audio': str(wav),
            'ref_text': text.read_text(encoding='utf-8').strip() if text.is_file() else None}


def resolve_reference_audio(value):
    """Accept reference files from the configured voice directory or legacy mount."""
    roots = [voice_directory().resolve(), Path('/data/voices').resolve()]
    path = Path(value).expanduser()
    candidates = [path.resolve()] if path.is_absolute() else [
        (root / path).resolve() for root in roots]
    for candidate in candidates:
        if candidate.is_file() and any(candidate.is_relative_to(root) for root in roots):
            return str(candidate)
    raise HTTPException(400, f'ref_audio must exist under {roots[0]}')
