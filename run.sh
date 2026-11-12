#!/bin/bash
# From the desktop: ssh -t rocbox /home/ubuntu/sound-server/src/run.sh
# HOST/PORT override 0.0.0.0:8000; S2S_LLM_URL overrides the chat upstream.
# MODELS_INI and SOUND_SERVER_PYTHON override the config and main interpreter.
# On a read-only checkout, voices/sessions are copied once to
# ${XDG_DATA_HOME:-$HOME/.local/share}/sound-server and saved there afterward.
# Extra arguments are passed to Uvicorn; one worker is always used.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${SOUND_SERVER_PYTHON:-$ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
    echo "Missing Python environment: $PYTHON (run ./install.sh or set SOUND_SERVER_PYTHON)." >&2
    exit 1
fi

cd "$ROOT"
export VIRTUAL_ENV="$(dirname -- "$(dirname -- "$PYTHON")")"
export PATH="$VIRTUAL_ENV/bin:$ROOT/bin/whisper/bin:$PATH"
export LD_LIBRARY_PATH="$ROOT/bin/whisper/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# The editable installs from Podman still point to /opt/qwen-{asr,tts}.
export PYTHONPATH="$ROOT/src:$ROOT/vendor/qwen-asr:$ROOT/vendor/qwen-tts${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON" - "$ROOT" "$@" <<'PY'
import configparser
import os
from pathlib import Path
import shutil
import sys
import sysconfig
import tempfile

from dotenv import dotenv_values
import uvicorn

root = Path(sys.argv[1])
file_env = {}
for name in ('.env', 'studio.env'):
    file_env.update(dotenv_values(root / name))
for key, value in file_env.items():
    if value is not None:
        os.environ.setdefault(key, value)

data_root = root / 'data'
if not os.access(data_root, os.W_OK):
    data_root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'sound-server'
    data_root.mkdir(parents=True, exist_ok=True)
    for name in ('voices', 'sessions'):
        source = root / 'data' / name
        target = data_root / name
        if source.is_dir() and not target.exists():
            shutil.copytree(source, target)


def local_path(value):
    for old, new in (('/data', data_root),
                     ('/opt/whisper', root / 'bin' / 'whisper')):
        if value == old or value.startswith(old + '/'):
            return str(new) + value[len(old):]
    return value


for key, default in (
    ('S2S_VOICE_DIR', str(data_root / 'voices/ANGEL')),
    ('S2S_SESSIONS_DIR', str(data_root / 'sessions')),
):
    os.environ[key] = local_path(os.environ.get(key, default))
    Path(os.environ[key]).mkdir(parents=True, exist_ok=True)

source = Path(os.environ.get('MODELS_INI', str(root / 'data/models.ini')))
config = configparser.ConfigParser(interpolation=None)
if not config.read(source):
    raise SystemExit(f'Cannot read model configuration: {source}')
for section in config.values():
    for key, value in list(section.items()):
        section[key] = local_path(value)

# Podman's service name is localhost when both services run inside rocbox.
if config.has_section('server'):
    for key in ('realtime_llm_base_url', 'studio_llm_base_url'):
        config['server'][key] = os.environ.get('S2S_LLM_URL', os.environ.get('LLM_BASE_URL', 'http://rocbox:18080/v1')).rstrip('/')

# Preserve the shared configuration used by Compose.
with tempfile.TemporaryDirectory(prefix='sound-server-') as runtime:
    omni_python = root / '.omnivoice-venv/bin/python'
    if 'OMNIVOICE_PYTHON' not in os.environ and omni_python.is_file():
        # The worker's installed .pth still references /usr/local/venv.
        # Append shared packages after its own Transformers 5, without editing
        # either environment (parts of the shared checkout are read-only).
        (Path(runtime) / 'sitecustomize.py').write_text(
            f'import site; site.addsitedir({sysconfig.get_path("purelib")!r})\n')
        os.environ['PYTHONPATH'] = runtime + os.pathsep + os.environ['PYTHONPATH']
        os.environ['OMNIVOICE_PYTHON'] = str(omni_python)
    target = Path(runtime) / 'models.ini'
    with target.open('w') as file:
        config.write(file)
    os.environ['MODELS_INI'] = str(target)
    uvicorn.main(args=[
        'server:app', '--host', os.environ.get('HOST', '0.0.0.0'),
        '--port', os.environ.get('PORT', '8000'), *sys.argv[2:],
        '--workers', '1',
    ])
PY
