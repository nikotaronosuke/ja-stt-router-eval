import hashlib
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from stt_eval.audio import AudioChunk
from stt_eval.live_worker import LiveStudyWorker

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class Engine:
    def __init__(self, *args, **kwargs):
        self.metadata = {'fake': True}

    async def open(self):
        pass

    async def close(self):
        pass

    async def release_unused_cache(self):
        pass

    async def _infer(self, pcm):
        return 'test', time.perf_counter_ns()

    async def begin(self, callback):
        self.callback = callback

    async def feed(self, chunk):
        self.callback('テスト', time.perf_counter_ns(), False)

    async def finish(self):
        self.callback('テスト', time.perf_counter_ns(), True)


class Sampler:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def stop(self):
        pass


class LiveStudyTests(unittest.TestCase):
    def test_live_chunks_and_saved_recording_have_identical_hash_and_clock(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            (root / 'fixtures' / 'manifests').mkdir(parents=True)
            with wave.open(str(root / 'test.wav'), 'wb') as writer:
                writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                writer.writeframes(bytes(6400))
            manifest = {'schema_version': 1, 'tests': [
                {'test_id': 'test', 'condition': 'unit', 'reference_text': 'test', 'audio_kind': 'synthetic_smoke',
                 'audio': '../../test.wav', 'keywords': [], 'decision_keywords': [], 'proper_nouns': []}]}
            (root / 'fixtures' / 'manifests' / 'synthetic-smoke.json').write_text(json.dumps(manifest), encoding='utf-8')
            with patch('stt_eval.live_worker.ParakeetEngine', Engine), patch('stt_eval.live_worker.ResourceSampler', Sampler):
                worker = LiveStudyWorker(root, root / 'out', .5)
                try:
                    until = time.monotonic() + 3
                    while not worker.ready and time.monotonic() < until:
                        time.sleep(.01)
                    self.assertTrue(worker.ready, worker.error)
                    worker.begin()
                    pcm = b'\x01\x00' * 320
                    when = time.perf_counter_ns()
                    worker.feed(AudioChunk(0, pcm, when, stream='loopback'))
                    worker.finish()
                    until = time.monotonic() + 3
                    while worker.result is None and time.monotonic() < until:
                        time.sleep(.01)
                    result = worker.result
                    self.assertEqual(result['canonical_sha256'], hashlib.sha256(pcm).hexdigest())
                    self.assertEqual(result['frames'], 320)
                    self.assertEqual(result['t0_ns'], when - 20000000)
                    self.assertEqual(result['metrics']['final_text'], 'テスト')
                finally:
                    worker.close()
                self.assertFalse((root / 'artifacts' / '.run.lock').exists())


if __name__ == '__main__':
    unittest.main()
