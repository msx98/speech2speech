"""Pipe-based OmniVoice worker, isolated from Qwen's Transformers 4 runtime."""
import base64
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import weakref

import numpy as np

_workers = weakref.WeakSet()


class OmniVoiceWorker:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.process = None
        self.lock = threading.Lock()
        _workers.add(self)

    @property
    def model(self):
        # Match the host's model cache interface for eviction and reactivation.
        return self

    def to(self, device):
        self.close()
        if str(device) != 'cpu':
            self.settings['device'] = str(device)
        return self

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()

    def generate(self, **request):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.close()
                python = self.settings.get('python', os.getenv(
                    'OMNIVOICE_PYTHON', '/usr/local/omnivoice-venv/bin/python'))
                if not Path(python).is_file():
                    raise RuntimeError('OmniVoice runtime is missing. Run ./install.sh and recreate the service.')
                worker = Path(__file__).with_name('omnivoice_worker.py')
                self.process = subprocess.Popen(
                    [python, '-u', str(worker)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, text=True, bufsize=1)
                request['load'] = self.settings
            try:
                self.process.stdin.write(json.dumps(request) + '\n')
                self.process.stdin.flush()
                with selectors.DefaultSelector() as ready:
                    ready.register(self.process.stdout, selectors.EVENT_READ)
                    if not ready.select(float(self.settings.get('timeout', '600'))):
                        raise TimeoutError('OmniVoice generation timed out')
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError('OmniVoice worker exited; check the server logs')
                response = json.loads(line)
                if response.get('error'):
                    raise RuntimeError(response['error'])
                audio = np.frombuffer(base64.b64decode(response['audio']), dtype='<f4').copy()
                return audio, int(response['sample_rate'])
            except Exception:
                self.close()
                raise


def close_workers():
    for worker in list(_workers):
        worker.close()
