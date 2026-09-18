"""The evaluation runner's arithmetic, and what it hands a router.

The property that matters most: the router is handed the candidate view and
nothing else, so no label and no description can leak into a provider that might
run off this machine.
"""
import json
import socket
import unittest

from router_eval.contract import CandidateView, Router, RouterInput, ScoredCandidate
from router_eval.dataset import Dataset, Question, load_dataset, validate_candidate
from router_eval.evaluate import OUTCOMES, classify, evaluate, report_to_dict
from router_eval.keyword import KeywordRouter


class RecordingRouter(Router):
    """Accepts everything it is given, and keeps the requests for inspection."""
    name = 'recording'
    score_type = 'stub_score'
    decision_policy = 'always the first candidate'

    def __init__(self):
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        first = request.candidates[0].id if request.candidates else None
        return first, ([ScoredCandidate(first, 1.0)] if first else []), {}


class AbstainRouter(Router):
    name = 'abstain'
    score_type = 'stub_score'
    decision_policy = 'never selects'

    def decide(self, request):
        return None, [], {}


class FixedRouter(Router):
    name = 'fixed'
    score_type = 's'

    def __init__(self, candidate_id):
        self.candidate_id = candidate_id

    def decide(self, request):
        alternatives = [ScoredCandidate(self.candidate_id, 1.0)] if self.candidate_id else []
        return self.candidate_id, alternatives, {}


def small_dataset(questions, triggers=(('e01', 'あ'), ('e02', 'い'))):
    candidates = tuple(validate_candidate({'id': cid, 'title': '架空' + cid, 'triggers': [term], 'description': '架空'})
                       for cid, term in triggers)
    return Dataset('small', candidates, CandidateView.many(candidates), tuple(questions), {})


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()

    def test_the_router_receives_candidate_views_and_no_labels(self):
        router = RecordingRouter()
        evaluate(router, self.data, keep_cases=False)
        self.assertEqual(len(router.requests), len(self.data.questions))
        request = router.requests[0]
        self.assertEqual(set(type(request).__dataclass_fields__), {'question', 'candidates', 'previous_question'})
        for view in request.candidates:
            self.assertIsInstance(view, CandidateView)
            self.assertEqual(set(type(view).__dataclass_fields__), {'id', 'title', 'triggers'})
        text = repr(router.requests)
        for leak in ('expected_ids', 'acceptable_ids', 'should_abstain', 'notes', 'description'):
            self.assertNotIn(leak, text, leak)
        for candidate in self.data.candidates:
            self.assertNotIn(candidate.description, text, candidate.id)

    def test_outcomes_are_five_exclusive_buckets(self):
        question = Question(id='x', category='direct', question='q', expected_ids=('e01',),
                            acceptable_ids=('e02',), should_abstain=False, notes='n')
        self.assertEqual(classify(question, FixedRouter('e01').route(RouterInput('q'))), 'expected_hit')
        self.assertEqual(classify(question, FixedRouter('e02').route(RouterInput('q'))), 'acceptable_hit')
        self.assertEqual(classify(question, FixedRouter('e09').route(RouterInput('q'))), 'wrong_display')
        self.assertEqual(classify(question, FixedRouter(None).route(RouterInput('q'))), 'missed_but_safe')
        abstaining = Question(**{**question.__dict__, 'expected_ids': (), 'should_abstain': True})
        self.assertEqual(classify(abstaining, FixedRouter(None).route(RouterInput('q'))), 'correct_abstain')
        self.assertEqual(classify(abstaining, FixedRouter('e01').route(RouterInput('q'))), 'wrong_display')

    def test_metrics_add_up_on_a_small_dataset(self):
        data = small_dataset((Question('q1', 'direct', 'あ', ('e01',), (), False, 'n'),
                              Question('q2', 'none', 'う', (), (), True, 'n'),
                              Question('q3', 'paraphrase', 'え', ('e02',), (), False, 'n')))
        report = evaluate(KeywordRouter(), data)
        self.assertEqual(sum(report.outcomes.values()), 3)
        self.assertEqual(report.outcomes['expected_hit'], 1)     # q1 matches 'あ'
        self.assertEqual(report.outcomes['correct_abstain'], 1)  # q2 matches nothing
        self.assertEqual(report.outcomes['missed_but_safe'], 1)  # q3 matches nothing but expected e02
        self.assertEqual(report.metrics['wrong_display_rate'], 0.0)
        self.assertEqual(report.metrics['correct_abstain_rate'], 1.0)
        self.assertEqual(report.metrics['expected_hit_rate'], 0.5)
        self.assertEqual(report.metrics['abstain_precision'], 0.5)
        self.assertEqual(report.metrics['counts'], {'should_abstain': 1, 'abstained': 2, 'with_expected': 2,
                                                    'false_positive': 0, 'false_negative': 1})
        self.assertIsNone(report.metrics['multiple_candidate_recall'])

    def test_multiple_candidate_recall_counts_the_alternatives(self):
        data = small_dataset((Question('q1', 'multiple', 'あ', ('e01', 'e02'), (), False, 'n'),),
                             triggers=(('e01', 'あ'), ('e02', 'あ')))
        # Keyword abstains on two matches, but both are reported as alternatives.
        report = evaluate(KeywordRouter(), data)
        self.assertEqual(report.outcomes['missed_but_safe'], 1)
        self.assertEqual(report.metrics['multiple_candidate_recall'], 1.0)
        self.assertEqual(report.metrics['wrong_display_rate'], 0.0)

    def test_any_router_can_be_evaluated_without_touching_the_dataset(self):
        for router in (KeywordRouter(), RecordingRouter(), AbstainRouter()):
            report = evaluate(router, self.data, keep_cases=False)
            self.assertEqual(report.total, 242)
            self.assertEqual(sum(report.outcomes.values()), 242)
            self.assertEqual(report.provider, router.name)
            self.assertEqual(set(report.outcomes), set(OUTCOMES))
        blanket = evaluate(AbstainRouter(), self.data, keep_cases=False)
        self.assertEqual(blanket.metrics['wrong_display_rate'], 0.0)   # abstaining is never harmful
        self.assertEqual(blanket.metrics['correct_abstain_rate'], 1.0)
        self.assertEqual(blanket.metrics['expected_hit_rate'], 0.0)    # and never useful

    def test_saved_results_carry_ids_only_and_no_question_text(self):
        report = evaluate(KeywordRouter(), self.data)
        text = json.dumps(report_to_dict(report), ensure_ascii=False)
        for question in self.data.questions:
            self.assertNotIn(question.question, text, question.id)
            self.assertNotIn(question.notes, text, question.id)
        for candidate in self.data.candidates:
            self.assertNotIn(candidate.title, text, candidate.id)
            self.assertNotIn(candidate.description, text, candidate.id)
        self.assertIn('wrong_display_rate', text)

    def test_keyword_baseline_is_stable_and_never_scores_probabilities(self):
        first = evaluate(KeywordRouter(), self.data, keep_cases=False)
        second = evaluate(KeywordRouter(), self.data, keep_cases=False)
        self.assertEqual(first.outcomes, second.outcomes)  # deterministic
        self.assertEqual(first.score_type, 'trigger_match_count')
        self.assertEqual(first.by_category['direct']['expected_hit'], 40)
        self.assertEqual(first.by_category['none']['correct_abstain'], 25)
        self.assertGreater(first.by_category['distractor']['wrong_display'], 0)
        # The report must not invent a probability-style metric out of provider scores.
        for key in report_to_dict(first, include_cases=False)['metrics']:
            self.assertNotIn(key, ('auc', 'brier', 'calibration', 'mean_score', 'confidence'))

    def test_summary_lines_are_printable(self):
        report = evaluate(KeywordRouter(), self.data, keep_cases=False)
        lines = report.summary_lines() + report.category_lines()
        self.assertTrue(any('wrong_display_rate' in line for line in lines))
        self.assertTrue(any(line.strip().startswith('direct') for line in lines))


class NoNetworkTests(unittest.TestCase):
    def test_a_full_evaluation_opens_no_socket(self):
        opened = []
        original = socket.socket.connect

        def guard(self, address, *args, **kwargs):
            opened.append(address)
            raise AssertionError('network_access_attempted')

        socket.socket.connect = guard
        try:
            evaluate(KeywordRouter(), load_dataset(), keep_cases=False)
        finally:
            socket.socket.connect = original
        self.assertEqual(opened, [])


if __name__ == '__main__':
    unittest.main()
