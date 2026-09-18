"""Join saved traces to independently reviewed, audio-bound keyword annotations.

    python scripts/keyword_end_report.py --manifest fixtures/manifests/natural-speech-reviewed.json \
        --run artifacts/interval-sweep/interval-250ms --output artifacts/keyword-end-250ms.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.audio import validate_manifest  # noqa: E402
from stt_eval.keyword_timing import keyword_end_metrics  # noqa: E402
from stt_eval.metrics import quantiles  # noqa: E402


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    tests = {test['test_id']: test for test in validate_manifest(args.manifest)}
    rows = read_jsonl(args.run / 'results.jsonl')
    events = {}
    for event in read_jsonl(args.run / 'events.jsonl'):
        if event['kind'] == 'transcript':
            events.setdefault((event['trial'], event['engine']), []).append(event)
    results = []
    for row in rows:
        test = tests.get(row['test_id'])
        if not test:
            continue
        result = keyword_end_metrics(events.get((row['trial'], row['engine']), []), row['t0_ns'], test,
                                     test.get('keyword_annotation'), row['canonical_sha256'], row['duration_s'])
        results.append({'trial': row['trial'], 'test_id': row['test_id'], 'engine': row['engine'],
                        'condition': row['condition'], 'pacing_max_ms': row.get('pacing_max_ms'), **result})
    aggregate = {}
    for engine in sorted({row['engine'] for row in results}):
        seen = set()
        latencies = []
        for row in results:
            if row['engine'] != engine or row['test_id'] in seen:
                continue
            seen.add(row['test_id'])
            latencies.extend(item['latency_ms'] for item in row['items'] if item['review'] == 'human_verified')
        aggregate[engine] = {'independent_test_count': len(seen), 'keyword_end_latency_ms': quantiles(latencies)}
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'rows': results, 'summary': aggregate,
                   'scope': 'first trial per unique test; human-verified spans only'},
                  stream, ensure_ascii=False, indent=2)
    print(json.dumps(aggregate))


if __name__ == '__main__':
    main()
