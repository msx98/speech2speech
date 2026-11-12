import importlib.util
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).parent / 'benchmarks' / 'realtime_conversation.py'
spec = importlib.util.spec_from_file_location('conversation_benchmark', path)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class RecordingTests(unittest.TestCase):
    def test_stereo_channels_gaps_and_queued_output(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'call.wav'
            recording = benchmark.ConversationRecording(target)
            with patch.object(benchmark.time, 'perf_counter', side_effect=[10.0, 10.5, 10.5]):
                recording.input(struct.pack('<hh', 100, 200))
                recording.output(struct.pack('<hh', 300, 400))
                recording.output(struct.pack('<h', 500))
            recording.save()
            with wave.open(str(target)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getframerate()), (2, 24000))
                samples = struct.unpack('<' + 'h' * wav.getnframes() * 2, wav.readframes(wav.getnframes()))
            self.assertEqual(samples[:4], (100, 0, 200, 0))
            self.assertEqual(samples[24000:24006], (0, 300, 0, 400, 0, 500))
            self.assertTrue(all(x == 0 for x in samples[4:24000]))

    def test_recording_is_optional(self):
        recording = benchmark.ConversationRecording(None)
        recording.input(bytes(4))
        recording.output(bytes(4))
        recording.save()
        self.assertEqual(recording.segments, [])

    def test_interruption_discards_queued_and_partial_audio(self):
        recording = benchmark.ConversationRecording(Path('unused.wav'))
        with patch.object(benchmark.time, 'perf_counter', side_effect=[10.0, 10.5, 10.5, 10.75]):
            recording.input(struct.pack('<h', 100))
            recording.output(struct.pack('<h', 300) * 24000)
            recording.output(struct.pack('<h', 400) * 24000)
            cutoff = recording.interrupt_output()
        self.assertEqual(cutoff, .75)
        self.assertEqual(recording.output_until, .75)
        self.assertEqual(len(recording.segments), 2)
        self.assertEqual(recording.segments[1], (1, .5, struct.pack('<h', 300) * 6000))


if __name__ == '__main__':
    unittest.main()
