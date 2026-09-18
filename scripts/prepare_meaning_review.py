"""Prepare a text-comparison table for manual meaning review, kept apart from raw results.

    python scripts/prepare_meaning_review.py --source artifacts/comparison-soak-65min/results.jsonl:medium \
        --source artifacts/openai-80-high/results.jsonl:high --output artifacts/meaning-review.json

Reference and hypothesis pairs are grouped into unique cases. Pairs whose normalized
strings are identical are marked 0 automatically; everything else stays `null` until
a person reviews it. Existing decisions in the output file are preserved. The
private reference/hypothesis text stays in the local review file and never reaches
tool output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.metrics import normalize  # noqa: E402

METHOD = ('Reference/final text comparison only; no independent listening. Count unit: utterances with '
          'meaning-breaking changes, 0 or 1. Spelling-only changes are 0. Ambiguous cases stay null.')


def parse_source(value):
    path, _, label = value.rpartition(':')
    if not path:
        raise argparse.ArgumentTypeError('expected PATH:LABEL')
    return Path(path), label


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=parse_source, action='append', required=True,
                        help='results.jsonl path and configuration label, as PATH:LABEL')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'meaning-review.json')
    args = parser.parse_args()
    if args.output.exists():
        data = json.loads(args.output.read_text(encoding='utf-8'))
    else:
        data = {'method': METHOD, 'cases': [], 'samples': []}
    by_key = {case['key']: case for case in data['cases']}
    seen = set()
    samples = []
    for source, configuration in args.source:
        if not source.exists():
            continue
        for line in source.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                break  # an active writer may have an incomplete final line
            identity = (configuration, row['engine'], row['test_id'])
            if identity in seen:
                continue
            seen.add(identity)
            if row['status'] != 'ok':
                continue
            reference, hypothesis = normalize(row['reference_text']), normalize(row['final_text'])
            key = hashlib.sha256((reference + '\n' + hypothesis).encode()).hexdigest()
            if key not in by_key:
                same = reference == hypothesis
                case = {'case': len(data['cases']) + 1, 'key': key, 'reference': row['reference_text'],
                        'hypothesis': row['final_text'], 'meaning_breaking_utterance': 0 if same else None,
                        'reviewer': 'normalized_text_identity' if same else 'pending',
                        'note': 'Normalized strings are identical.' if same else ''}
                data['cases'].append(case)
                by_key[key] = case
            samples.append({'engine': row['engine'], 'test_id': row['test_id'], 'condition': row['condition'],
                            'configuration': configuration, 'case': by_key[key]['case']})
    data['samples'] = samples
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    pending = [case for case in data['cases'] if case['meaning_breaking_utterance'] is None]
    print(json.dumps({'samples': len(samples), 'unique_cases': len(data['cases']), 'pending': len(pending)}))


if __name__ == '__main__':
    main()
