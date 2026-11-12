"""Observe Transformers token emission without changing inference results.

The caller must hold the model's inference lock for this context's lifetime.
Transformers calls streamer.put once for the prompt, then for generated tokens.
"""
from contextlib import contextmanager


@contextmanager
def observe_tokens(model, callback, *, tokenizer=None, on_text=None):
    original = model.generate
    missing = object()
    previous = vars(model).get('generate', missing)

    class Observer:
        def __init__(self, downstream):
            self.prompt = True
            self.downstream = downstream
            self.ids = []
            self.text = None

        def put(self, value):
            if self.prompt:
                self.prompt = False
            else:
                callback('token')
                if on_text is not None:
                    self.ids.extend(value.reshape(-1).tolist())
                    raw = tokenizer.decode(self.ids, skip_special_tokens=False)
                    if '<asr_text>' in raw:
                        text = raw.split('<asr_text>', 1)[1]
                        for special in tokenizer.all_special_tokens:
                            text = text.replace(special, '')
                        text = text.lstrip()
                        if text and not text.endswith('\ufffd') and text != self.text:
                            self.text = text
                            on_text(text)
            if self.downstream is not None:
                self.downstream.put(value)

        def end(self):
            callback('end')
            if self.downstream is not None:
                self.downstream.end()

    def generate(*args, **kwargs):
        kwargs['streamer'] = Observer(kwargs.get('streamer'))
        callback('start')
        return original(*args, **kwargs)

    model.generate = generate
    try:
        yield
    finally:
        if previous is missing:
            del model.generate
        else:
            model.generate = previous
