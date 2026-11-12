"""Codec chunk boundaries and cancellation without loading model weights."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen_stream import GenerationCancelled, stream_custom_voice


class FakeTalker:
    def __init__(self):
        self.hooks = []

    def register_forward_hook(self, hook):
        self.hooks.append(hook)
        return SimpleNamespace(remove=lambda: self.hooks.remove(hook))

    def frame(self, codes):
        for hook in list(self.hooks):
            hook(self, (), SimpleNamespace(hidden_states=(None, codes)))


class FakeTokenizer:
    def __init__(self):
        self.windows = []

    def get_decode_upsample_rate(self):
        return 2

    def decode(self, items):
        codes = items[0]['audio_codes'][:, 0].cpu().numpy()
        self.windows.append(list(codes))
        return [np.repeat(codes, 2).astype(np.float32)], 24000


class FakeModel:
    def __init__(self, count):
        self.count = count
        self.model = SimpleNamespace(talker=FakeTalker(), speech_tokenizer=FakeTokenizer(),
            config=SimpleNamespace(talker_config=SimpleNamespace(codec_eos_token_id=999)))

    def generate_custom_voice(self, **kwargs):
        self.model.talker.frame(None)
        for i in range(self.count):
            self.model.talker.frame(torch.tensor([[i, i]]))
        self.model.talker.frame(torch.tensor([[999, 0]]))
        return [np.repeat(np.arange(self.count), 2).astype(np.float32)], 24000


class StreamingTests(unittest.TestCase):
    def test_chunk_boundaries_context_and_final_tail(self):
        for count in (1, 7, 8, 9, 64, 67):
            with self.subTest(count=count):
                model = FakeModel(count)
                output = []
                stream_custom_voice(model, 'test', 'Ryan', 'English', None,
                    lambda a, rate: output.append(a), threading.Event())
                np.testing.assert_array_equal(np.concatenate(output), np.repeat(np.arange(count), 2))
                self.assertFalse(model.model.talker.hooks)
                self.assertTrue(all(len(window) <= 33 for window in model.model.speech_tokenizer.windows))

    def test_cancellation_removes_hook_and_model_can_be_reused(self):
        model = FakeModel(40)
        cancelled = threading.Event()
        def callback(audio, rate):
            cancelled.set()
        with self.assertRaises(GenerationCancelled):
            stream_custom_voice(model, 'test', 'Ryan', 'English', None, callback, cancelled)
        self.assertFalse(model.model.talker.hooks)
        cancelled.clear()
        output = []
        stream_custom_voice(model, 'test', 'Ryan', 'English', None,
            lambda a, rate: output.append(a), cancelled)
        self.assertEqual(len(np.concatenate(output)), 80)

    def test_callback_error_removes_hook(self):
        model = FakeModel(16)
        def fail(audio, rate):
            raise RuntimeError('client disconnected')
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            stream_custom_voice(model, 'test', 'Ryan', 'English', None, fail, threading.Event())
        self.assertFalse(model.model.talker.hooks)


if __name__ == '__main__':
    unittest.main()
