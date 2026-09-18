"""Recompute live meeting-route CER and keyword-end latency only after human review.

    python scripts/live_route_report.py --output artifacts/live-route-report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.keyword_timing import captured_keyword_end_times, keyword_end_metrics  # noqa: E402
from stt_eval.metrics import TranscriptTrace, guard_reviewed_keyword_reference  # noqa: E402
from stt_eval.spelling import equivalence_scores  # noqa: E402

QUALITY_KEYS = ('cer', 'keyword_hit', 'keyword_recall', 'proper_noun_hits', 'proper_noun_accuracy')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, default=ROOT / 'fixtures' / 'manifests' / 'natural-speech.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for test in json.loads(args.manifest.read_text(encoding='utf-8'))['tests']:
        if not test.get('live_trace'):
            continue
        trace_path = (ROOT / test['live_trace']).resolve()
        if ROOT not in trace_path.parents:
            raise ValueError('trace path escapes project')
        trace = json.loads(trace_path.read_text(encoding='utf-8'))
        if trace['canonical_sha256'] != test['canonical_sha256']:
            raise ValueError('live trace audio mismatch')
        metrics = TranscriptTrace(trace['t0_ns'], trace['events']).result(
            test['reference_text'], test['keywords'], test['proper_nouns'], test['decision_keywords'])
        if test['reference_review'] != 'verified':
            for key in QUALITY_KEYS:
                metrics[key] = None
        guard_reviewed_keyword_reference(metrics, test)
        annotation = test['keyword_annotation']
        timing = keyword_end_metrics(trace['events'], trace['t0_ns'], test, annotation, trace['canonical_sha256'],
                                     trace['frames'] / 16000, captured_keyword_end_times(annotation, trace['timeline']))
        rows.append({'test_id': test['test_id'], 'route': test['route'], 'condition': test['condition'],
                     'reference_review': test['reference_review'], 'interval_s': trace['interval_s'],
                     'queue_max_ms': trace['queue_max_ms'], 'keyword_end': timing, **metrics,
                     **equivalence_scores(test, metrics['final_text'])})
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'rows': rows}, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'live_clips': len(rows), 'reviewed': sum(row['reference_review'] == 'verified' for row in rows)}))


if __name__ == '__main__':
    main()
