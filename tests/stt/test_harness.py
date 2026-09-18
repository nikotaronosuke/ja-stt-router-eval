import asyncio
import json
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from stt_eval.engines import OpenAIEngine
from stt_eval.harness import RunLock, compare
from stt_eval.report import summarize, write_csv
from stt_eval.safety import AudioBudget, DiagnosticError

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class FakeSampler:
    samples = []

    def __init__(self, *args):
        pass

    def start(self):
        return self

    def stop(self):
        pass

    def summary(self, *args):
        return {}


class FakeEngine:
    model = 'unit-test-only'
    metadata = {'hints': False}

    def __init__(self, name):
        self.name = name
        self.emit = lambda event: None
        self.chunks = []

    async def open(self):
        pass

    async def begin(self, callback):
        self.callback = callback

    async def feed(self, chunk):
        self.chunks.append(chunk)
        if len(self.chunks) == 2:
            self.callback('天気', time.perf_counter_ns(), False)

    async def finish(self):
        self.callback('天気予報', time.perf_counter_ns(), True)

    async def abort(self):
        pass

    async def close(self):
        pass


class HarnessTest(unittest.IsolatedAsyncioTestCase):
    async def test_identical_audio_timestamps_and_report(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            with wave.open(str(root / 'test.wav'), 'wb') as writer:
                writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                writer.writeframes(bytes(6400))
            manifest = {'schema_version': 1, 'tests': [{
                'test_id': 'test', 'audio': 'test.wav', 'reference_text': '天気予報', 'condition': 'unit_test',
                'audio_kind': 'synthetic_smoke', 'keywords': ['天気予報'], 'proper_nouns': [],
                'decision_keywords': ['天気予報']}]}
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            a, b = FakeEngine('fake_a'), FakeEngine('fake_b')
            with patch('stt_eval.harness.ResourceSampler', FakeSampler):
                run = await compare(root / 'manifest.json', root / 'out', [a, b])
            self.assertEqual(a.chunks, b.chunks)
            self.assertEqual(sum(len(chunk.pcm16) for chunk in a.chunks), 38400)
            self.assertTrue((root / 'out' / 'summary.md').exists())
            self.assertEqual(run['soak_status'], '未計測')
            rows = [json.loads(line) for line in (root / 'out' / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['canonical_sha256'], rows[1]['canonical_sha256'])
            self.assertGreater(rows[0]['keyword_arrival_ms'], 1000)

    async def test_session_uses_current_model_and_plural_languages(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            engine = OpenAIEngine(AudioBudget(Path(directory) / 'ledger', 5), allow_network=True, hints=True)
            transcription = engine.session_update()['session']['audio']['input']['transcription']
            self.assertEqual(transcription['model'], 'gpt-live-transcribe')
            self.assertEqual(transcription['languages'], ['ja'])
            self.assertNotIn('language', transcription)
            self.assertIn('keywords', transcription)

    async def test_cloud_is_explicit(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            with self.assertRaises(PermissionError):
                OpenAIEngine(AudioBudget(Path(directory) / 'ledger', 5))

    async def test_failure_is_in_denominator(self):
        rows = [{'engine': 'x', 'condition': 'a', 'audio_kind': 'human', 'hints': False,
                 'status': 'error', 'cer': 0, 'keyword_recall': 1, 'proper_noun_accuracy': 1,
                 'first_partial_ms': 5, 'keyword_arrival_ms': 5, 'stable_ms': 5}]
        result = summarize(rows)[0]
        self.assertIsNone(result['cer'])
        self.assertEqual(result['keyword_arrival_ms']['missing'], 1)

    async def test_invalid_duration_cannot_start_unbounded_run(self):
        with self.assertRaises(ValueError):
            await compare(Path('unused'), Path('unused'), [FakeEngine('test')], duration_s=float('nan'))

    async def test_csv_protects_formula_with_leading_whitespace(self):
        import csv
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            path = Path(directory) / 'test.csv'
            write_csv(path, [{'text': '\t=1+1', 'number': -2.5}])
            with path.open(encoding='utf-8-sig', newline='') as stream:
                result = list(csv.DictReader(stream))[0]
            self.assertTrue(result['text'].startswith("'"))
            self.assertEqual(result['number'], '-2.5')


class RunLockTest(unittest.TestCase):
    def test_stale_lock_is_replaced_only_when_owner_is_gone(self):
        import psutil
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            lock = root / 'artifacts' / '.run.lock'
            lock.parent.mkdir()
            dead = max(psutil.pids()) + 100000
            while psutil.pid_exists(dead):
                dead += 1
            lock.write_text(str(dead), encoding='ascii')
            with RunLock(root):
                self.assertEqual(lock.read_text(encoding='ascii'), str(os.getpid()))
            self.assertFalse(lock.exists())
            for content, code in ((str(os.getpid()), 'run_lock_held'), ('', 'run_lock_unreadable'),
                                  ('not-a-pid', 'run_lock_unreadable'), ('0', 'run_lock_held')):
                lock.write_text(content, encoding='ascii')
                with self.assertRaises(DiagnosticError) as raised:
                    with RunLock(root):
                        pass
                self.assertEqual(str(raised.exception), code)
                self.assertEqual(lock.read_text(encoding='ascii'), content)
            lock.unlink()
            with RunLock(root):
                with self.assertRaises(DiagnosticError):
                    with RunLock(root):
                        pass
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())


if __name__ == '__main__':
    unittest.main()
