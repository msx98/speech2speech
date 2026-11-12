"""Streaming acoustic echo cancellation for clock-aligned microphone/playback PCM.

The browser supplies the actually rendered playback bus beside microphone PCM,
in the same audio callback. SpeexDSP learns the acoustic/device delay and room
response; no wall-clock or network alignment is used. One instance owns one call.
"""
import ctypes
import ctypes.util
from functools import lru_cache
import sys

import numpy as np
from scipy.signal import correlate


class EchoCancellationUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _library():
    path = ctypes.util.find_library('speexdsp') or 'libspeexdsp.so.1'
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise EchoCancellationUnavailable('Install libspeexdsp1 for server echo cancellation') from exc
    lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_echo_state_init.restype = ctypes.c_void_p
    lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
    lib.speex_echo_state_destroy.restype = None
    lib.speex_echo_state_reset.argtypes = [ctypes.c_void_p]
    lib.speex_echo_state_reset.restype = None
    lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lib.speex_echo_ctl.restype = ctypes.c_int
    samples = ctypes.POINTER(ctypes.c_int16)
    lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, samples, samples, samples]
    lib.speex_echo_cancellation.restype = None
    return lib


class EchoCanceller:
    """Adaptive cancellation preserving near-end speech, with 10 ms reframing.

    Input is mono little-endian PCM16. Both channels must have identical lengths.
    process() may buffer fewer than one frame; output preserves sample order and
    has at most 10 ms additional framing latency. No reference means PCM silence,
    not a microphone mute. The filter's history survives turns and replacements.
    """
    @staticmethod
    def available():
        try:
            _library()
            return True
        except EchoCancellationUnavailable:
            return False

    def __init__(self, sample_rate=24000, tail_ms=300):
        if sample_rate not in (8000, 16000, 24000, 32000, 48000):
            raise ValueError('Unsupported echo cancellation sample rate')
        if not 50 <= tail_ms <= 500:
            raise ValueError('Echo tail must be between 50 and 500 ms')
        if sys.byteorder != 'little':
            raise EchoCancellationUnavailable('Server echo cancellation requires little-endian PCM')
        self.sample_rate, self.tail_ms = sample_rate, tail_ms
        self.frame_samples = sample_rate // 100
        self.frame_bytes = self.frame_samples * 2
        self._mic, self._reference = bytearray(), bytearray()
        self._active_frames = self.frames_processed = 0
        self._history = np.zeros(sample_rate * tail_ms // 1000 + self.frame_samples, dtype=np.float64)
        self.bootstrap_frames = 0
        self._lib = _library()
        self._state = self._lib.speex_echo_state_init(self.frame_samples, sample_rate * tail_ms // 1000)
        if not self._state:
            raise MemoryError('Cannot allocate acoustic echo filter')
        rate = ctypes.c_int(sample_rate)
        if self._lib.speex_echo_ctl(self._state, 24, ctypes.byref(rate)) != 0:
            self.close()
            raise EchoCancellationUnavailable('Cannot configure acoustic echo sample rate')
        self._frame_type = ctypes.c_int16 * self.frame_samples

    def _bootstrap(self, mic, ref, cleaned):
        """Remove a strongly matched direct echo while the adaptive FIR learns.

        This subtracts only a scaled, delayed playback waveform. Uncorrelated
        near-end speech remains in the residual; no microphone gating is used.
        Speex still trains on the original microphone, including during startup.
        """
        self._history[:-self.frame_samples] = self._history[self.frame_samples:]
        self._history[-self.frame_samples:] = np.frombuffer(ref, dtype='<i2')
        signal = np.frombuffer(mic, dtype='<i2').astype(np.float64)
        energy = float(signal @ signal)
        if energy < self.frame_samples * 16 ** 2:
            return bytes(cleaned)
        sums = np.concatenate(([0.], np.cumsum(self._history ** 2)))
        powers = sums[self.frame_samples:] - sums[:-self.frame_samples]
        dots = correlate(self._history, signal, mode='valid', method='fft')
        correlations = np.abs(dots) / np.sqrt(np.maximum(powers * energy, 1.))
        correlations[powers < self.frame_samples * 16 ** 2] = 0.
        index = int(np.argmax(correlations))
        if correlations[index] < .8:
            return bytes(cleaned)
        gain = dots[index] / max(powers[index], 1.)
        if not .02 <= abs(gain) <= 2.:
            return bytes(cleaned)
        residual = signal - gain * self._history[index:index + self.frame_samples]
        native = np.frombuffer(cleaned, dtype='<i2').astype(np.float64)
        if residual @ residual >= native @ native:
            return bytes(cleaned)
        self.bootstrap_frames += 1
        return np.clip(np.rint(residual), -32768, 32767).astype('<i2').tobytes()

    def process(self, microphone, playback_reference=None):
        if not self._state:
            raise RuntimeError('Echo canceller is closed')
        if len(microphone) % 2:
            raise ValueError('PCM16 requires even byte counts')
        if playback_reference is None:
            playback_reference = bytes(len(microphone))
        if len(playback_reference) != len(microphone):
            raise ValueError('Microphone and playback reference lengths must match')
        self._mic.extend(microphone)
        self._reference.extend(playback_reference)
        output = bytearray()
        complete = len(self._mic) // self.frame_bytes * self.frame_bytes
        for start in range(0, complete, self.frame_bytes):
            end = start + self.frame_bytes
            mic = self._mic[start:end]
            ref = self._reference[start:end]
            if any(ref):
                self._active_frames = self.tail_ms // 10 + 1
            if self._active_frames:
                cleaned = self._frame_type()
                self._lib.speex_echo_cancellation(
                    self._state, self._frame_type.from_buffer_copy(mic),
                    self._frame_type.from_buffer_copy(ref), cleaned)
                output.extend(self._bootstrap(mic, ref, cleaned))
                self._active_frames -= 1
            else:
                self._history[:-self.frame_samples] = self._history[self.frame_samples:]
                self._history[-self.frame_samples:] = 0
                output.extend(mic)
            self.frames_processed += 1
        del self._mic[:complete]
        del self._reference[:complete]
        return bytes(output)

    def reset(self):
        if not self._state:
            raise RuntimeError('Echo canceller is closed')
        self._lib.speex_echo_state_reset(self._state)
        self._mic.clear()
        self._reference.clear()
        self._active_frames = self.frames_processed = 0
        self.bootstrap_frames = 0
        self._history.fill(0)

    def close(self):
        if getattr(self, '_state', None):
            self._lib.speex_echo_state_destroy(self._state)
            self._state = None
        self._mic.clear()
        self._reference.clear()

    def __del__(self):
        if hasattr(self, '_mic'):
            self.close()
