"""Write the seal for a router evaluation set.

    python scripts/seal_router_fixture.py              # seal fixtures/router-eval-v1
    python scripts/seal_router_fixture.py --reseal     # replace an existing seal on purpose

The seal records the SHA-256 of `candidates.json` and `questions.json` together with
the counts. `load_dataset` refuses a directory whose files no longer match, so an edit
after a provider run cannot silently keep old results attached to a changed dataset.
Re-sealing is deliberate: it is refused unless `--reseal` is given, and the sealed_on
date is renewed, which is the signal that earlier results belong to a different set.

Local only: reads and writes one fixture directory, no network, no personal data.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from router_eval.dataset import DEFAULT_DIR, SEAL_FILE, SEALED_FILES, file_digest, load_dataset  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description='Seal a router evaluation set')
    parser.add_argument('--dataset', default=None, help='dataset directory (default: fixtures/router-eval-v1)')
    parser.add_argument('--reseal', action='store_true', help='replace an existing seal')
    parser.add_argument('--date', default=None, help='sealed_on date (default: today, ISO format)')
    args = parser.parse_args(argv)
    directory = Path(args.dataset) if args.dataset else DEFAULT_DIR
    seal_path = directory / SEAL_FILE
    if seal_path.exists() and not args.reseal:
        raise SystemExit(f'{seal_path.name} exists; pass --reseal to replace it on purpose')
    dataset = load_dataset(directory, require_seal=False)
    seal = {
        'dataset': dataset.name,
        'sealed_on': args.date or date.today().isoformat(),
        'candidates': len(dataset.candidates),
        'questions': len(dataset.questions),
        'files': {name: file_digest(directory / name) for name in SEALED_FILES},
        'note': 'Sealed before any provider comparison. Do not edit the fixture after a provider run; '
                'a changed fixture is a new dataset and earlier results do not compare to it.',
    }
    seal_path.write_text(json.dumps(seal, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'sealed {dataset.name}: {seal["candidates"]} candidates, {seal["questions"]} questions '
          f'({seal["sealed_on"]})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
