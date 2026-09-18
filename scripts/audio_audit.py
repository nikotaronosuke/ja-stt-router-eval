"""Local recording integrity and level audit; infers no transcripts or truth labels.

    python scripts/audio_audit.py --output artifacts/audio-audit.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.audio import load_fixture, validate_manifest  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, default=ROOT / 'fixtures' / 'manifests' / 'natural-speech.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for test in validate_manifest(args.manifest):
        pcm, meta = load_fixture(test['_audio_path'])
        values = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
        rows.append({'test_id': test['test_id'], 'route': test.get('route'), 'condition': test['condition'],
                     'duration_s': meta['duration_s'],
                     'canonical_hash_matches': meta['canonical_sha256'] == test.get('canonical_sha256'),
                     'rms': float(np.sqrt(np.mean(values * values))), 'peak': float(np.max(np.abs(values))),
                     'clipped_fraction': float(np.mean(np.abs(values) >= .999)),
                     'reference_review': test.get('reference_review')})
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'rows': rows}, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'count': len(rows), 'all_hashes_match': all(row['canonical_hash_matches'] for row in rows),
                      'duration_range_s': [min(row['duration_s'] for row in rows), max(row['duration_s'] for row in rows)],
                      'zero_signal_clips': sum(row['peak'] == 0 for row in rows),
                      'maximum_clipped_fraction': max(row['clipped_fraction'] for row in rows)}))


if __name__ == '__main__':
    main()
