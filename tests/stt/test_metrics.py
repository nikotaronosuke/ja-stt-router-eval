import unittest

from stt_eval.metrics import (NATURAL_SPEECH_SOURCE, TranscriptTrace, cer, guard_reviewed_keyword_reference, hits,
                              normalize, quantiles)


class MetricsTest(unittest.TestCase):
    def test_japanese_normalization(self):
        self.assertEqual(normalize('Ｗｅｂ メディア、ＡＩ。'), 'webメディアai')
        self.assertEqual(cer('天気予報', '電気予報'), .25)

    def test_missing_reference_not_zero(self):
        self.assertIsNone(cer('', 'anything'))

    def test_aliases(self):
        self.assertEqual(hits('シェアポイントで情報共有', [['SharePoint', 'シェアポイント'], '情報共有']), [True, True])

    def test_missing_partial_and_final_not_substituted(self):
        trace = TranscriptTrace(100)
        trace.add('天気予報', 1000000100, True)
        result = trace.result('天気予報', ['天気予報'], [])
        self.assertIsNone(result['first_partial_ms'])
        self.assertEqual(result['final_ms'], 1000)
        self.assertIsNone(result['proper_noun_accuracy'])

    def test_revisions_stable_and_keyword_time(self):
        trace = TranscriptTrace(0)
        for text, ms, final in [('電気', 100, False), ('天気', 200, False), ('天気予報', 400, False),
                                ('天気余報', 600, False), ('天気予報', 800, False), ('天気予報。', 1000, True)]:
            trace.add(text, ms * 1000000, final)
        result = trace.result('天気予報', ['天気', '予報'], [])
        self.assertEqual(result['partial_revision_count'], 3)
        self.assertEqual(result['keyword_arrival_ms'], 400)
        self.assertEqual(result['stable_ms'], 800)
        self.assertEqual(result['cer'], 0)

    def test_unfinished_trace_is_unmeasured(self):
        trace = TranscriptTrace(0)
        trace.add('天気', 1000000)
        result = trace.result('天気予報', ['天気予報'], [])
        self.assertIsNone(result['final_text'])
        self.assertIsNone(result['cer'])
        self.assertIsNone(result['stable_ms'])
        self.assertIsNone(result['keyword_arrival_ms'])

    def test_quantiles_include_missing_denominator(self):
        result = quantiles([100, 200, None, 300])
        self.assertEqual(result, {'count': 3, 'total': 4, 'missing': 1, 'p50': 200, 'p90': 300, 'p95': 300, 'max': 300})

    def test_clock_order_checked(self):
        trace = TranscriptTrace(100)
        with self.assertRaises(ValueError):
            trace.add('a', 99)

    def test_elicited_prompt_is_not_evidence_the_word_was_spoken(self):
        metrics = {'keyword_hit': [True], 'keyword_recall': 1.0, 'proper_noun_hits': [], 'proper_noun_accuracy': None}
        absent = {'source': NATURAL_SPEECH_SOURCE, 'reference_review': 'verified',
                  'reference_text': '別の言葉だけを話した', 'keywords': ['天気予報']}
        guarded = guard_reviewed_keyword_reference(dict(metrics), absent)
        self.assertEqual(guarded['keyword_reference_status'], 'unverified_or_absent')
        self.assertIsNone(guarded['keyword_recall'])
        present = {**absent, 'reference_text': '天気予報を確認した'}
        self.assertEqual(guard_reviewed_keyword_reference(dict(metrics), present)['keyword_recall'], 1.0)
        other = {'source': 'fixture', 'reference_text': 'x', 'keywords': ['天気予報']}
        self.assertNotIn('keyword_reference_status', guard_reviewed_keyword_reference(dict(metrics), other))


if __name__ == '__main__':
    unittest.main()
