import unittest

from stt_eval.keyword_timing import captured_keyword_end_times, keyword_end_metrics
from stt_eval.metrics import NATURAL_SPEECH_SOURCE


class KeywordTimingTests(unittest.TestCase):
    def test_natural_speech_accepts_only_explicit_spelling_alias(self):
        annotation = {'canonical_sha256': 'hash', 'spans': [{'keyword_index': 0, 'term': 'SharePoint',
                                                              'start_s': .2, 'end_s': .8, 'review': 'human_verified'}]}
        event = {'text': 'シェアポイント', 't_ns': 2000000000, 'final': False}
        test = {'keywords': ['SharePoint'], 'source': NATURAL_SPEECH_SOURCE}
        result = keyword_end_metrics([event], 1000000000, test, annotation, 'hash', 1.)
        self.assertEqual(result['latency_ms']['p50'], 200.)
        result = keyword_end_metrics([event], 1000000000, {'keywords': ['SharePoint']}, annotation, 'hash', 1.)
        self.assertEqual(result['latency_ms']['missing'], 1)

    def measure(self, events, **changes):
        annotation = {'canonical_sha256': 'hash', 'spans': [{'keyword_index': 0, 'term': '企画室',
                                                              'start_s': 1.2, 'end_s': 2., 'review': 'human_verified',
                                                              **changes}]}
        return keyword_end_metrics(events, 1000000000, {'keywords': ['企画室']}, annotation, 'hash', 3.)

    def test_end_to_arrival_is_not_utterance_start(self):
        result = self.measure([{'text': '企画室です', 't_ns': 3300000000, 'final': False}])
        self.assertEqual(result['latency_ms']['p50'], 300)

    def test_early_prediction_keeps_signed_latency(self):
        result = self.measure([{'text': '企画室', 't_ns': 2800000000, 'final': False}])
        self.assertEqual(result['latency_ms']['p50'], -200)
        self.assertTrue(result['items'][0]['early_prediction'])

    def test_missing_is_not_zero_and_unreviewed_is_excluded(self):
        self.assertEqual(self.measure([])['latency_ms']['missing'], 1)
        self.assertEqual(self.measure([], review='model_estimate')['latency_ms']['total'], 0)

    def test_invalid_span_or_repeated_occurrence_is_rejected(self):
        for values in [{'end_s': 4.}, {'occurrence': 2}, {'term': 'other'}]:
            with self.assertRaises(ValueError):
                self.measure([], **values)

    def test_hash_mismatch_is_rejected(self):
        annotation = {'canonical_sha256': 'other', 'spans': []}
        with self.assertRaises(ValueError):
            keyword_end_metrics([], 0, {'keywords': ['x']}, annotation, 'hash', 1.)

    def test_live_keyword_clock_uses_received_block_not_a_nominal_start(self):
        annotation = {'spans': [{'keyword_index': 0, 'end_s': .03}]}
        times = captured_keyword_end_times(annotation, [
            {'start_frame': 0, 'end_frame': 320, 'block_received_ns': 120000000},
            {'start_frame': 320, 'end_frame': 640, 'block_received_ns': 145000000}])
        self.assertEqual(times[0], 135000000)


if __name__ == '__main__':
    unittest.main()
