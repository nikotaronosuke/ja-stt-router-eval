"""OpenAIRouter: contract compliance, what the request may contain, and the send gate.

Every test here injects a fake transport, so the suite never opens a socket and
never reads a real key. The point of most of them is negative: the things that
must not be in the request, and the things the router must not be able to return.
"""
import json
import os
import socket
import unittest

from router_eval.contract import CandidateView, RouterInput, RouterResult, ScoredCandidate, comparable
from router_eval.dataset import DEFAULT_DIR, Dataset, load_dataset, validate_candidate
from router_eval.evaluate import evaluate
from router_eval.keyword import KeywordRouter
from router_eval.openai_router import (DECISION_SCHEMA, DEFAULT_MODEL, INSTRUCTIONS, BudgetExceeded,
                                       MissingApiKey, OpenAIRouter, api_key_present, build_input,
                                       build_payload, extract_text)
from router_eval.precheck import estimate, precheck


def reply(selected='e01', alternatives=(('e02', 0.4),), abstain=False, review=False,
          input_tokens=900, output_tokens=40, cached=0):
    body = {'selected_id': selected, 'abstain': abstain, 'review': review,
            'alternatives': [{'candidate_id': cid, 'score': score} for cid, score in alternatives]}
    return {'status': 'completed', 'output_text': json.dumps(body, ensure_ascii=False),
            'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                      'cache_read_input_tokens': cached}}


class Fake:
    """Records the payloads it is given and returns a canned reply."""

    def __init__(self, *replies):
        self.payloads = []
        self.replies = list(replies) or [reply()]

    def __call__(self, payload):
        self.payloads.append(payload)
        return self.replies[min(len(self.payloads) - 1, len(self.replies) - 1)]


def with_candidates(data, candidates):
    return Dataset(data.name, tuple(candidates), CandidateView.many(candidates), data.questions, data.seal)


class RequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()

    def request(self, question='会議室を予約したいのですが、どうすればいいですか。', previous=''):
        return RouterInput(question=question, candidates=self.data.views, previous_question=previous)

    def test_the_payload_matches_the_documented_responses_api_shape(self):
        payload = build_payload(self.request())
        self.assertEqual(payload['model'], DEFAULT_MODEL)
        self.assertEqual(payload['text']['format']['type'], 'json_schema')
        self.assertEqual(payload['text']['format']['strict'], True)
        self.assertEqual(payload['text']['format']['schema'], DECISION_SCHEMA)
        self.assertEqual(payload['reasoning'], {'effort': 'none'})
        self.assertIs(payload['store'], False)   # no application state retained
        self.assertIs(payload['stream'], False)
        self.assertEqual(payload['max_output_tokens'], 256)
        self.assertEqual(payload['instructions'], INSTRUCTIONS)
        self.assertEqual(set(payload), {'model', 'instructions', 'input', 'text', 'reasoning',
                                        'store', 'stream', 'max_output_tokens'})

    def test_a_non_default_effort_gets_room_for_reasoning_tokens(self):
        self.assertEqual(build_payload(self.request(), effort='low')['max_output_tokens'], 2048)

    def test_only_id_title_and_triggers_leave_the_machine(self):
        sent = build_payload(self.request())['input']
        for candidate in self.data.candidates:
            self.assertIn(candidate.id, sent)
            self.assertIn(candidate.title, sent)
            self.assertNotIn(candidate.description, sent)
        for leak in ('description', 'expected_ids', 'acceptable_ids', 'should_abstain', 'notes'):
            self.assertNotIn(leak, sent, leak)

    def test_no_evaluation_label_is_ever_sent(self):
        for question in self.data.questions[:40]:
            payload = build_payload(RouterInput(question=question.question, candidates=self.data.views,
                                                previous_question=question.previous_question))
            sent = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(question.notes, sent)
            for key in ('expected_ids', 'acceptable_ids', 'should_abstain', 'category'):
                self.assertNotIn(key, sent, key)

    def test_the_previous_question_is_included_only_when_present(self):
        self.assertIn('（なし）', build_input(self.request()))
        with_context = build_input(self.request('いつから？', '会議室を予約したいのですが、どうすればいいですか。'))
        self.assertIn('直前の発話: 会議室を予約したいのですが、どうすればいいですか。', with_context)
        self.assertIn('現在の発話: いつから？', with_context)

    def test_the_schema_and_instructions_forbid_prose(self):
        properties = set(DECISION_SCHEMA['properties'])
        self.assertEqual(properties, {'selected_id', 'alternatives', 'abstain', 'review'})
        for banned in ('answer', 'suggested_answer', 'generated_response', 'script', 'text', 'reply'):
            self.assertNotIn(banned, properties)
        self.assertIs(DECISION_SCHEMA['additionalProperties'], False)
        self.assertEqual(DECISION_SCHEMA['required'], ['selected_id', 'alternatives', 'abstain', 'review'])
        self.assertIn('回答文', INSTRUCTIONS)      # explicitly told not to write one
        self.assertIn('abstain', INSTRUCTIONS)


class DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()

    def route(self, fake, question='会議室を予約したいのですが、どうすればいいですか。'):
        router = OpenAIRouter(transport=fake)
        result = router.route(RouterInput(question=question, candidates=self.data.views))
        return router, result

    def test_a_selection_becomes_a_contract_result(self):
        _, result = self.route(Fake(reply('e05', (('e06', 0.3),))))
        self.assertIsInstance(result, RouterResult)
        self.assertEqual(result.selected_id, 'e05')
        self.assertFalse(result.abstain)
        self.assertEqual(result.score_type, 'openai_relevance_0_1')
        self.assertEqual(result.alternatives, (ScoredCandidate('e06', 0.3),))
        self.assertEqual(result.metadata['review'], False)
        self.assertEqual(result.metadata['effort'], 'none')
        self.assertGreaterEqual(result.latency_ms, 0.0)

    def test_the_model_asking_to_abstain_is_honoured(self):
        _, result = self.route(Fake(reply(None, (), abstain=True)))
        self.assertIsNone(result.selected_id)
        self.assertTrue(result.abstain)

    def test_inconsistent_or_unknown_ids_abstain_rather_than_guess(self):
        for body in (reply('e05', (), abstain=True),      # says abstain but also picks
                     reply('nonexistent', ()),           # id not in the candidate set
                     reply(None, ())):                   # null without the flag
            _, result = self.route(Fake(body))
            self.assertIsNone(result.selected_id)
            self.assertTrue(result.abstain)

    def test_unknown_and_duplicate_alternatives_are_dropped(self):
        _, result = self.route(Fake(reply('e05', (('e06', 0.3), ('zz9', 0.9), ('e06', 0.2), ('e04', 0.1)))))
        self.assertEqual([item.candidate_id for item in result.alternatives], ['e06', 'e04'])

    def test_scores_stay_provider_native(self):
        _, openai_result = self.route(Fake(reply('e05', (('e06', 0.8),))))
        keyword = KeywordRouter().route(RouterInput(question='会議室を予約したいのですが。',
                                                    candidates=self.data.views))
        self.assertFalse(comparable(openai_result, keyword))
        self.assertNotEqual(openai_result.score_type, keyword.score_type)

    def test_usage_and_cost_accumulate_per_request(self):
        fake = Fake(reply(input_tokens=1000, output_tokens=50, cached=200))
        router = OpenAIRouter(transport=fake)
        for _ in range(3):
            router.route(RouterInput(question='駐車場はありますか。', candidates=self.data.views))
        self.assertEqual(router.usage, {'requests': 3, 'input_tokens': 3000,
                                        'cached_input_tokens': 600, 'output_tokens': 150})
        # 800 fresh input at $0.20/M + 200 cached at $0.02/M + 50 output at $1.20/M, three times
        expected = 3 * ((800 * 0.20) + (200 * 0.02) + (50 * 1.20)) / 1_000_000
        self.assertAlmostEqual(router.cost_usd, expected, places=10)

    def test_prices_can_be_overridden_per_run(self):
        router = OpenAIRouter(transport=Fake(reply(input_tokens=1_000_000, output_tokens=0)),
                              prices={'input': 1.0, 'output': 0.0, 'cached': 0.0})
        router.route(RouterInput(question='駐車場はありますか。', candidates=self.data.views))
        self.assertAlmostEqual(router.cost_usd, 1.0, places=10)

    def test_the_budget_stops_further_sending(self):
        router = OpenAIRouter(transport=Fake(reply(input_tokens=2_000_000, output_tokens=0)), budget_usd=0.10)
        router.route(RouterInput(question='駐車場はありますか。', candidates=self.data.views))
        self.assertGreater(router.cost_usd, 0.10)
        with self.assertRaises(BudgetExceeded):
            router.route(RouterInput(question='別の発話です。', candidates=self.data.views))
        self.assertEqual(router.usage['requests'], 1)  # the second request was never sent

    def test_refusals_and_truncation_are_reported_not_guessed(self):
        with self.assertRaises(RuntimeError) as truncated:
            extract_text({'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}})
        self.assertIn('max_output_tokens', str(truncated.exception))
        with self.assertRaises(RuntimeError) as refused:
            extract_text({'status': 'completed', 'output': [{'content': [{'type': 'refusal', 'refusal': 'no'}]}]})
        self.assertEqual(str(refused.exception), 'openai_refusal')
        with self.assertRaises(RuntimeError):
            extract_text({'status': 'completed', 'output': []})

    def test_a_missing_key_fails_before_any_network_access(self):
        saved = os.environ.get('OPENAI_API_KEY')
        os.environ['OPENAI_API_KEY'] = '   '
        try:
            self.assertFalse(api_key_present())
            from router_eval.openai_router import _api_key
            with self.assertRaises(MissingApiKey):
                _api_key()
        finally:
            if saved is None:
                os.environ.pop('OPENAI_API_KEY', None)
            else:
                os.environ['OPENAI_API_KEY'] = saved

    def test_no_error_message_can_carry_the_key(self):
        saved = os.environ.get('OPENAI_API_KEY')
        os.environ['OPENAI_API_KEY'] = 'dummy-value-not-a-real-key-000000'
        try:
            def explode(payload):
                raise RuntimeError('openai_http_401')

            router = OpenAIRouter(transport=explode)
            with self.assertRaises(RuntimeError) as error:
                router.route(RouterInput(question='q', candidates=self.data.views))
            self.assertNotIn('dummy-value', str(error.exception))
            self.assertNotIn('dummy-value', repr(router.__dict__))
        finally:
            if saved is None:
                os.environ.pop('OPENAI_API_KEY', None)
            else:
                os.environ['OPENAI_API_KEY'] = saved


class PrecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()

    def test_the_sealed_dataset_passes(self):
        self.assertEqual(precheck(self.data, OpenAIRouter(transport=Fake()), DEFAULT_DIR), [])

    def test_a_tampered_dataset_is_refused(self):
        short = Dataset(self.data.name, self.data.candidates, self.data.views, self.data.questions[:10], self.data.seal)
        findings = precheck(short, OpenAIRouter(transport=Fake()))
        self.assertTrue(any('question_count' in item for item in findings), findings)
        renamed = Dataset('other-set', self.data.candidates, self.data.views, self.data.questions, self.data.seal)
        findings = precheck(renamed, OpenAIRouter(transport=Fake()))
        self.assertTrue(any('dataset_is_not_sealed' in item for item in findings))

    def test_a_broken_seal_is_reported_when_the_directory_is_given(self):
        seal = {**self.data.seal, 'files': {**self.data.seal['files'], 'questions.json': '0' * 64}}
        stale = Dataset(self.data.name, self.data.candidates, self.data.views, self.data.questions, seal)
        findings = precheck(stale, OpenAIRouter(transport=Fake()), DEFAULT_DIR)
        self.assertIn('seal_mismatch:questions.json', findings)

    def test_a_candidate_carrying_forbidden_material_is_refused(self):
        candidates = list(self.data.candidates)
        first = candidates[0]
        candidates[0] = validate_candidate({'id': first.id, 'title': '連絡先 tester@example.com の架空候補',
                                            'triggers': list(first.triggers), 'description': first.description})
        findings = precheck(with_candidates(self.data, candidates), OpenAIRouter(transport=Fake()))
        self.assertTrue(any('email_in_payload' in item for item in findings), findings)

    def test_candidate_body_reaching_the_payload_would_be_caught(self):
        candidates = list(self.data.candidates)
        first = candidates[0]
        # A title that accidentally embeds the candidate's own description.
        candidates[0] = validate_candidate({'id': first.id, 'title': first.description[:75],
                                            'triggers': list(first.triggers), 'description': first.description[:75]})
        findings = precheck(with_candidates(self.data, candidates), OpenAIRouter(transport=Fake()))
        self.assertTrue(any('candidate_body_in_payload' in item for item in findings), findings)

    def test_a_candidate_without_the_fictional_marker_is_refused(self):
        candidates = list(self.data.candidates)
        first = candidates[0]
        candidates[0] = validate_candidate({'id': first.id, 'title': first.title,
                                            'triggers': list(first.triggers), 'description': '実在に見える説明'})
        findings = precheck(with_candidates(self.data, candidates), OpenAIRouter(transport=Fake()))
        self.assertIn(f'candidate_missing_fictional_marker:{first.id}', findings)

    def test_the_estimate_is_an_upper_bound_within_budget(self):
        numbers = estimate(self.data, OpenAIRouter(transport=Fake()))
        self.assertEqual(numbers['requests'], 242)
        self.assertEqual(numbers['output_tokens_cap_per_request'], 256)
        self.assertTrue(numbers['within_budget'])
        self.assertLess(numbers['total_usd_worst_case'], 0.50)
        self.assertGreater(numbers['input_tokens_per_request'], 0)


class NoNetworkTests(unittest.TestCase):
    def test_the_whole_evaluation_runs_on_a_fake_transport_without_a_socket(self):
        opened = []
        original = socket.socket.connect

        def guard(self, address, *args, **kwargs):
            opened.append(address)
            raise AssertionError('network_access_attempted')

        socket.socket.connect = guard
        try:
            data = load_dataset()
            router = OpenAIRouter(transport=Fake(reply(None, (), abstain=True)))
            report = evaluate(router, data, keep_cases=False)
        finally:
            socket.socket.connect = original
        self.assertEqual(opened, [])
        self.assertEqual(report.total, 242)
        self.assertEqual(router.usage['requests'], 242)
        self.assertEqual(report.provider, 'openai')
        self.assertEqual(report.metrics['wrong_display_rate'], 0.0)  # abstaining is never harmful


if __name__ == '__main__':
    unittest.main()
