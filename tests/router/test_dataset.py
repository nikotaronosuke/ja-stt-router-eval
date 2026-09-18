"""The sealed dataset: its own invariants, and the seal that guards it.

Everything here is fictional. The sealed fixture is read in place; the seal tests
copy it to a temporary directory before editing anything.
"""
import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from router_eval.dataset import (CATEGORIES, DEFAULT_DIR, SEAL_FILE, load_dataset, read_seal,
                                 seal_findings, validate_candidate)

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)

EXPECTED_COUNTS = {'direct': 40, 'paraphrase': 40, 'indirect': 30, 'follow_up': 22, 'multiple': 20,
                   'none': 25, 'ambiguous': 18, 'distractor': 20, 'scope_trap': 15, 'very_short': 12}


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()

    def test_the_fixture_is_the_sealed_set(self):
        self.assertEqual(self.data.name, 'ja-router-eval-v1')
        self.assertEqual(len(self.data.candidates), 16)
        self.assertEqual(len(self.data.questions), 242)
        self.assertEqual(self.data.seal['candidates'], 16)
        self.assertEqual(self.data.seal['questions'], 242)
        self.assertEqual(seal_findings(DEFAULT_DIR, read_seal(DEFAULT_DIR), self.data.name, 16, 242), [])

    def test_every_candidate_is_well_formed_and_fictional(self):
        self.assertEqual(len({candidate.id for candidate in self.data.candidates}), 16)
        for candidate in self.data.candidates:
            self.assertTrue(candidate.triggers, candidate.id)
            self.assertTrue(candidate.title, candidate.id)
            self.assertIn('架空', candidate.description, candidate.id)
            for term in candidate.triggers:
                self.assertEqual(term, term.casefold(), candidate.id)  # stored normalized

    def test_questions_are_labelled_consistently(self):
        self.assertEqual(len({question.id for question in self.data.questions}), 242)
        ids = {candidate.id for candidate in self.data.candidates}
        for question in self.data.questions:
            self.assertIn(question.category, CATEGORIES)
            self.assertTrue(question.question.strip())
            self.assertTrue(question.notes.strip(), question.id)  # every label must be explainable
            self.assertTrue(set(question.expected_ids) <= ids and set(question.acceptable_ids) <= ids, question.id)
            self.assertFalse(set(question.expected_ids) & set(question.acceptable_ids), question.id)
            if question.should_abstain:
                self.assertEqual(question.expected_ids, (), question.id)
            else:
                self.assertTrue(question.expected_ids, question.id)

    def test_the_set_covers_every_candidate_and_every_category(self):
        used = {item for question in self.data.questions for item in question.expected_ids + question.acceptable_ids}
        self.assertEqual(used, {candidate.id for candidate in self.data.candidates})
        counts = Counter(question.category for question in self.data.questions)
        self.assertEqual(dict(counts), EXPECTED_COUNTS)
        # The context-dependent categories must actually carry context, except for the
        # deliberate no-context cases that should abstain.
        for name in ('follow_up', 'very_short'):
            rows = [question for question in self.data.questions if question.category == name]
            self.assertTrue(all(question.previous_question or question.should_abstain for question in rows), name)
        multiple = [question for question in self.data.questions if question.category == 'multiple']
        self.assertTrue(all(len(question.expected_ids) >= 2 for question in multiple))
        for name in ('none', 'distractor', 'scope_trap', 'ambiguous'):
            rows = [question for question in self.data.questions if question.category == name]
            self.assertTrue(all(question.should_abstain for question in rows), name)


class SealTests(unittest.TestCase):
    def copy_fixture(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name) / 'router-eval-v1'
        shutil.copytree(DEFAULT_DIR, directory)
        return directory

    def test_a_question_edited_without_resealing_is_rejected(self):
        directory = self.copy_fixture()
        self.assertEqual(len(load_dataset(directory).questions), 242)
        path = directory / 'questions.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['questions'][0]['question'] = '書き換えられた架空の発話'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            load_dataset(directory)
        self.assertIn('seal_mismatch:questions.json', str(refused.exception))
        self.assertEqual(len(load_dataset(directory, require_seal=False).questions), 242)

    def test_a_candidate_edited_without_resealing_is_rejected(self):
        directory = self.copy_fixture()
        path = directory / 'candidates.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['candidates'][0]['title'] = '書き換えられた架空タイトル'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            load_dataset(directory)
        self.assertIn('seal_mismatch:candidates.json', str(refused.exception))

    def test_a_missing_seal_is_rejected_by_default(self):
        directory = self.copy_fixture()
        (directory / SEAL_FILE).unlink()
        with self.assertRaises(ValueError) as refused:
            load_dataset(directory)
        self.assertIn('seal_missing', str(refused.exception))

    def test_a_seal_with_the_wrong_counts_is_rejected(self):
        directory = self.copy_fixture()
        path = directory / SEAL_FILE
        seal = json.loads(path.read_text(encoding='utf-8'))
        seal['questions'] = 241
        path.write_text(json.dumps(seal), encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            load_dataset(directory)
        self.assertIn('seal_question_count_mismatch', str(refused.exception))


class CandidateValidationTests(unittest.TestCase):
    def test_terms_are_normalized_and_deduplicated(self):
        candidate = validate_candidate({'id': 'e01', 'title': '  架空  ', 'triggers': ['Ｗｉ-Ｆｉ', 'wi-fi', ' ', 'a b'],
                                        'description': '架空'})
        self.assertEqual(candidate.title, '架空')
        self.assertEqual(candidate.triggers, ('wi-fi', 'a b'))

    def test_bad_entries_are_refused(self):
        base = {'id': 'e01', 'title': '架空', 'triggers': ['x'], 'description': ''}
        for change, code in (({'id': 'E01'}, 'invalid_candidate_id'), ({'title': ''}, 'title_required'),
                             ({'triggers': []}, 'trigger_required'), ({'triggers': 'x'}, 'invalid_triggers'),
                             ({'triggers': ['x' * 41]}, 'trigger_too_long'),
                             ({'description': 'x' * 401}, 'description_too_long')):
            with self.assertRaises(ValueError) as refused:
                validate_candidate({**base, **change})
            self.assertIn(code, str(refused.exception))


if __name__ == '__main__':
    unittest.main()
