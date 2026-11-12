"""Internal worker: one JSON request/response per line, no network listener."""
import base64
import json
import sys
import traceback


def main():
    protocol = sys.stdout
    # Model libraries may print progress. Keep stdout exclusively for the protocol.
    sys.stdout = sys.stderr
    model = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if model is None:
                from omnivoice import OmniVoice
                import torch
                settings = request.pop('load')
                dtype = {'float16': torch.float16, 'bfloat16': torch.bfloat16,
                         'float32': torch.float32}[settings.get('dtype', 'float16')]
                model = OmniVoice.from_pretrained(
                    settings['path'], device_map=settings.get('device', 'cuda:0'),
                    dtype=dtype, load_asr=False, local_files_only=True)
            import numpy as np
            # Saved profiles supply their transcript; no additional ASR model is loaded.
            if request.get('ref_audio') and not request.get('ref_text'):
                raise ValueError('OmniVoice profiles need reference text. Transcribe the clips in Config and rebuild the reference.')
            audio = model.generate(**request)[0]
            response = {'audio': base64.b64encode(np.asarray(audio, dtype='<f4').tobytes()).decode(),
                        'sample_rate': int(model.sampling_rate)}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = {'error': str(exc)}
        protocol.write(json.dumps(response) + '\n')
        protocol.flush()


if __name__ == '__main__':
    main()
