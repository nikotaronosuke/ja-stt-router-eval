import json
import tempfile
import unittest
import wave
from pathlib import Path

from stt_eval.recorder import save_fixture

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class FixtureTests(unittest.TestCase):
    def test_recorded_takes_preserve_audio_and_require_reference_review(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            test = {'test_id': 'q01', 'reference_text': '質問', 'keywords': ['質問'],
                    'proper_nouns': [], 'decision_keywords': ['質問']}
            first = bytes(6400)
            path = save_fixture(root, test, 'normal', first)
            save_fixture(root, test, 'normal', bytes(9600))
            tests = json.loads(path.read_text(encoding='utf-8'))['tests']
            self.assertEqual(len(tests), 2)
            self.assertNotEqual(tests[0]['audio'], tests[1]['audio'])
            self.assertEqual(tests[0]['reference_review'], 'read_aloud_prompt_requires_review')
            with wave.open(str((path.parent / tests[0]['audio']).resolve()), 'rb') as reader:
                self.assertEqual(reader.readframes(reader.getnframes()), first)

    def test_test_id_cannot_escape_fixture_directory(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            with self.assertRaises(ValueError):
                save_fixture(Path(directory), {'test_id': '../../outside'}, 'normal', bytes(3200))

    def test_unknown_condition_and_bad_pcm_are_refused(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            test = {'test_id': 'q01'}
            with self.assertRaises(ValueError):
                save_fixture(Path(directory), test, 'shouting', bytes(3200))
            with self.assertRaises(ValueError):
                save_fixture(Path(directory), test, 'normal', bytes(3201))


if __name__ == '__main__':
    unittest.main()
