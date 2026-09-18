import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from stt_eval.audio import StreamingResampler, load_fixture, validate_manifest
from stt_eval.safety import AudioBudget, local_worker_environment, safe_error

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class ResamplerTests(unittest.TestCase):
    def test_resampler_split_matches_whole(self):
        rng = np.random.default_rng(5)
        source = rng.normal(size=9000) * .02
        for rates in [(16000, 24000), (48000, 16000), (44100, 16000)]:
            whole = StreamingResampler(*rates).process(source)
            split = StreamingResampler(*rates)
            output = np.concatenate([split.process(block) for block in np.array_split(source, 127)])
            np.testing.assert_allclose(whole, output, atol=1e-12)

    def test_upsampling_dc_and_length(self):
        output = StreamingResampler(16000, 24000).process(np.ones(16000) * .1)
        self.assertEqual(len(output), 24000)
        self.assertAlmostEqual(float(output[100:].mean()), .1, places=5)

    def test_causal_filter_tail_and_arbitrary_block_boundaries(self):
        for rate in (44100, 48000):
            values = np.zeros(rate // 10)
            values[0] = .5
            values[-1] = .5
            # Tail is explicit fixture padding, not invented historical samples.
            padded = np.concatenate([values, np.zeros(rate // 50)])
            converter = StreamingResampler(rate, 16000)
            expected = converter.process(padded)
            converter = StreamingResampler(rate, 16000)
            actual = np.concatenate([converter.process(block) for block in np.array_split(padded, 137)])
            np.testing.assert_allclose(actual, expected, atol=1e-12)
            self.assertEqual(len(actual), 1920)
            self.assertGreater(np.max(np.abs(actual[1600:])), .01)
            self.assertLess(converter.delay_ms, 1)


class FixtureTests(unittest.TestCase):
    def test_same_fixture_hash(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            path = Path(directory) / 'test.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(bytes(32000))
            pcm, meta = load_fixture(path)
            self.assertEqual(pcm, bytes(32000))
            self.assertEqual(meta['duration_s'], 1)

    def test_manifest_requires_metadata_and_audio_kind(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            test = {'test_id': 'a', 'audio': 'a.wav', 'reference_text': 'x', 'condition': 'c',
                    'audio_kind': 'human', 'keywords': [], 'proper_nouns': [], 'decision_keywords': []}
            path = root / 'manifest.json'
            path.write_text(json.dumps({'schema_version': 1, 'tests': [test]}), encoding='utf-8')
            self.assertEqual(validate_manifest(path, require_audio=False)[0]['test_id'], 'a')
            with self.assertRaises(FileNotFoundError):
                validate_manifest(path)
            bad = {**test, 'audio_kind': 'unknown'}
            path.write_text(json.dumps({'schema_version': 1, 'tests': [bad]}), encoding='utf-8')
            with self.assertRaises(ValueError):
                validate_manifest(path, require_audio=False)


class SafetyTests(unittest.TestCase):
    def test_budget_persists_and_stops_before_excess(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            path = Path(directory) / 'budget.json'
            AudioBudget(path, .02).reserve(60)
            other = AudioBudget(path, .02)
            with self.assertRaises(RuntimeError):
                other.reserve(60)
            self.assertAlmostEqual(json.loads(path.read_text())['estimated_usd'], .017)

    def test_budget_rejects_invalid_numbers(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            for number in [float('nan'), -1, float('inf')]:
                with self.assertRaises(ValueError):
                    AudioBudget(Path(directory) / 'budget.json', number)

    def test_error_does_not_leak_message(self):
        self.assertEqual(safe_error(ValueError('private-message')), 'ValueError')

    def test_child_environment_drops_unknown_credentials_without_mutating_parent(self):
        source = {'PATH': 'test-path', 'SYSTEMROOT': 'test-root', 'OPENAI_API_KEY': 'dummy-secret',
                  'UNKNOWN_SERVICE_TOKEN': 'dummy-other', 'WSLENV': 'OPENAI_API_KEY', 'PYTHONPATH': 'bad'}
        env = local_worker_environment(source)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('UNKNOWN_SERVICE_TOKEN', env)
        self.assertNotIn('WSLENV', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(source['OPENAI_API_KEY'], 'dummy-secret')
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')


if __name__ == '__main__':
    unittest.main()
