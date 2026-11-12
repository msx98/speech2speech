import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from token_timing import observe_tokens


class FakeModel:
    def generate(self, *, streamer=None, fail=False):
        streamer.put('prompt')
        streamer.put('token1')
        if fail:
            raise RuntimeError('inference failed')
        streamer.put('token2')
        streamer.end()
        return 'unchanged result'


class TokenTimingTests(unittest.TestCase):
    def test_prompt_is_excluded_and_method_restored(self):
        model = FakeModel()
        events = []
        with observe_tokens(model, events.append):
            self.assertEqual(model.generate(), 'unchanged result')
        self.assertEqual(events, ['start', 'token', 'token', 'end'])
        self.assertNotIn('generate', vars(model))

    def test_restored_after_failure(self):
        model = FakeModel()
        with self.assertRaisesRegex(RuntimeError, 'inference failed'):
            with observe_tokens(model, lambda kind: None):
                model.generate(fail=True)
        self.assertNotIn('generate', vars(model))

    def test_existing_streamer_is_preserved(self):
        model = FakeModel()
        class Streamer:
            def __init__(self): self.values = []
            def put(self, value): self.values.append(value)
            def end(self): self.values.append('end')
        streamer = Streamer()
        with observe_tokens(model, lambda kind: None):
            model.generate(streamer=streamer)
        self.assertEqual(streamer.values, ['prompt', 'token1', 'token2', 'end'])

    def test_transcript_tokens_are_forwarded_without_asr_control_tokens(self):
        import torch
        class Tokenizer:
            all_special_tokens = ['<|im_end|>']
            pieces = {1: 'language', 2: ' English', 3: '<asr_text>', 4: 'hello', 5: ' world', 6: '<|im_end|>'}
            def decode(self, ids, **kwargs):
                return ''.join(self.pieces[i] for i in ids)
        class Model:
            def generate(self, *, streamer):
                streamer.put(torch.tensor([[99]]))
                for token in range(1, 7):
                    streamer.put(torch.tensor([token]))
                streamer.end()
        text = []
        model = Model()
        with observe_tokens(model, lambda kind: None, tokenizer=Tokenizer(), on_text=text.append):
            model.generate()
        self.assertEqual(text, ['hello', 'hello world'])


if __name__ == '__main__':
    unittest.main()
