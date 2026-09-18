"""The router contract and the keyword baseline.

Fictional candidates and fictional questions only. Nothing here opens a network
connection.
"""
import socket
import unittest

from router_eval.contract import (FORBIDDEN_FIELDS, CandidateView, Router, RouterInput, RouterResult,
                                  ScoredCandidate, comparable, contract_violations)
from router_eval.dataset import validate_candidate
from router_eval.keyword import KeywordRouter

RESULT_FIELDS = {'provider', 'score_type', 'selected_id', 'alternatives', 'abstain', 'latency_ms', 'metadata'}


def candidate(**overrides):
    value = {'id': 'e01', 'title': '架空の受付案内', 'triggers': ['架空受付', '架空の手順'],
             'description': '架空の受付の案内文。routerには渡らない。'}
    value.update(overrides)
    return validate_candidate(value)


def views(candidates):
    return CandidateView.many(candidates)


class ContractTests(unittest.TestCase):
    def test_the_result_type_cannot_carry_an_answer(self):
        self.assertEqual(contract_violations(), [])
        names = set(RouterResult.__dataclass_fields__)
        self.assertEqual(names & FORBIDDEN_FIELDS, set())
        self.assertEqual(names, RESULT_FIELDS)
        result = RouterResult(provider='keyword', score_type='trigger_match_count', selected_id='e01', abstain=False)
        with self.assertRaises(Exception):
            result.selected_id = 'e02'  # frozen
        with self.assertRaises(Exception):
            result.answer = '架空の回答文'  # no such field
        for bad in ('answer', 'suggested_answer', 'recommended_script', 'script'):
            with self.assertRaises(ValueError):
                RouterResult(provider='p', score_type='t', selected_id='e01', abstain=False, metadata={bad: 'x'})
        # Metadata is diagnostics: short scalars only, so prose cannot be smuggled through.
        with self.assertRaises(ValueError):
            RouterResult(provider='p', score_type='t', selected_id='e01', abstain=False, metadata={'note': 'あ' * 81})
        with self.assertRaises(ValueError):
            RouterResult(provider='p', score_type='t', selected_id='e01', abstain=False, metadata={'note': ['a']})

    def test_abstain_and_selection_cannot_disagree(self):
        with self.assertRaises(ValueError):
            RouterResult(provider='p', score_type='t', selected_id='e01', abstain=True)
        with self.assertRaises(ValueError):
            RouterResult(provider='p', score_type='t', selected_id=None, abstain=False)

    def test_router_only_sees_the_search_view_of_a_candidate(self):
        full = candidate()
        view = CandidateView.of(full)
        self.assertEqual(view.id, 'e01')
        self.assertEqual(view.triggers, ('架空受付', '架空の手順'))
        self.assertEqual(set(CandidateView.__dataclass_fields__), {'id', 'title', 'triggers'})
        self.assertFalse(hasattr(view, 'description'))
        from_dict = CandidateView.of({'id': 'e02', 'title': 't', 'triggers': ['a'], 'description': 'hidden'})
        self.assertEqual(from_dict, CandidateView(id='e02', title='t', triggers=('a',)))

    def test_scores_are_provider_native_and_not_compared_across_types(self):
        keyword = RouterResult(provider='keyword', score_type='trigger_match_count', selected_id='e01',
                               abstain=False, alternatives=(ScoredCandidate('e01', 2.0),))
        other = RouterResult(provider='future', score_type='model_self_reported', selected_id='e01',
                             abstain=False, alternatives=(ScoredCandidate('e01', 0.8),))
        self.assertTrue(comparable(keyword, keyword))
        self.assertFalse(comparable(keyword, other))
        self.assertNotEqual(keyword.alternatives[0].score, other.alternatives[0].score)

    def test_a_provider_declares_its_score_type_and_route_times_it(self):
        class StubRouter(Router):
            name = 'stub'
            score_type = 'stub_score'
            decision_policy = 'always the first candidate'
            threshold = 0.5

            def decide(self, request):
                first = request.candidates[0].id if request.candidates else None
                return first, [ScoredCandidate(first, 1.0)] if first else [], {'considered': len(request.candidates)}

        result = StubRouter().route(RouterInput(question='架空受付の話', candidates=views([candidate()])))
        self.assertEqual(result.provider, 'stub')
        self.assertEqual(result.score_type, 'stub_score')
        self.assertFalse(result.abstain)
        self.assertGreaterEqual(result.latency_ms, 0.0)
        self.assertEqual(result.metadata['considered'], 1)
        with self.assertRaises(NotImplementedError):
            Router().route(RouterInput(question='x'))


class KeywordRouterTests(unittest.TestCase):
    def test_direct_match_selects_the_candidate(self):
        router = KeywordRouter()
        result = router.route(RouterInput(question='架空受付の準備について教えてください。',
                                          candidates=views([candidate()])))
        self.assertEqual(result.selected_id, 'e01')
        self.assertFalse(result.abstain)
        self.assertEqual(result.score_type, 'trigger_match_count')
        self.assertEqual(result.alternatives[0].candidate_id, 'e01')
        self.assertEqual(result.alternatives[0].score, 1.0)
        self.assertEqual(result.provider, 'keyword')

    def test_no_match_and_ambiguity_both_abstain(self):
        router = KeywordRouter()
        one = views([candidate()])
        miss = router.route(RouterInput(question='まったく関係のない架空の話題です。', candidates=one))
        self.assertIsNone(miss.selected_id)
        self.assertTrue(miss.abstain)
        self.assertEqual(miss.alternatives, ())
        both = views([candidate(), candidate(id='e02', title='別の架空候補', triggers=['架空受付'])])
        tie = router.route(RouterInput(question='架空受付の話', candidates=both))
        self.assertTrue(tie.abstain)
        self.assertIsNone(tie.selected_id)
        self.assertEqual(len(tie.alternatives), 2)  # both are reported, neither is chosen
        self.assertTrue(router.route(RouterInput(question='架空受付', candidates=())).abstain)

    def test_matching_ignores_width_and_case(self):
        router = KeywordRouter()
        wifi = views([candidate(triggers=['Wi-Fi'])])
        for text in ('Wi-Fiは使えますか', 'ＷＩ-ＦＩは使えますか', 'wi-fi は？'):
            self.assertEqual(router.route(RouterInput(question=text, candidates=wifi)).selected_id, 'e01', text)

    def test_the_score_counts_matched_terms(self):
        router = KeywordRouter()
        result = router.route(RouterInput(question='架空受付の架空の手順', candidates=views([candidate()])))
        self.assertEqual(result.alternatives[0].score, 2.0)
        self.assertEqual(result.metadata, {'matched': 1, 'considered': 1})


class NoNetworkTests(unittest.TestCase):
    def test_routing_opens_no_socket(self):
        opened = []
        original = socket.socket.connect

        def guard(self, address, *args, **kwargs):
            opened.append(address)
            raise AssertionError('network_access_attempted')

        socket.socket.connect = guard
        try:
            KeywordRouter().route(RouterInput(question='架空受付の話', candidates=views([candidate()])))
        finally:
            socket.socket.connect = original
        self.assertEqual(opened, [])


if __name__ == '__main__':
    unittest.main()
