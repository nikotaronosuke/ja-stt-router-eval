"""Select the first saved take for each prompt/route, preserving every source take.

    python scripts/freeze_first_takes.py --output fixtures/manifests/natural-speech-reviewed.json

The selection is quality-blind: it happens before any recognizer output is looked
at, so it cannot favour clips that happen to score well.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'fixtures' / 'manifests' / 'natural-speech.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    tests = json.loads(args.source.read_text(encoding='utf-8'))['tests']
    selected = []
    seen = set()
    excluded = []
    for test in tests:
        prompt, _ = test['test_id'].rsplit('_', 1)
        if prompt in seen:
            excluded.append({'test_id': test['test_id'], 'reason': 'later_take_same_prompt_route'})
            continue
        seen.add(prompt)
        row = dict(test)
        audio = (args.source.parent / test['audio']).resolve()
        row['audio'] = os.path.relpath(audio, args.output.resolve().parent).replace('\\', '/')
        selected.append(row)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'schema_version': 1, 'tests': selected,
                   'selection': {'rule': 'first_saved_take_per_prompt_and_route', 'excluded': excluded,
                                 'quality_blind': True, 'all_original_recordings_preserved': True}},
                  stream, ensure_ascii=False, indent=2)
    print(json.dumps({'selected': len(selected), 'excluded': len(excluded)}))


if __name__ == '__main__':
    main()
