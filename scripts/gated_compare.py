"""Memory-gated local benchmark; no raw exception or transcript reaches the console.

    python scripts/gated_compare.py --engine parakeet --count 5 --output artifacts/gated/parakeet-5.json
    python scripts/gated_compare.py --engine sherpa --count 5 --output artifacts/gated/sherpa-5.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.gated_benchmark import DEFAULT_MANIFEST, ENGINES, MAX_CLIPS, compare  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', choices=ENGINES, required=True)
    parser.add_argument('--count', type=int, default=5, help=f'clips to replay (1..{MAX_CLIPS})')
    parser.add_argument('--manifest', default=DEFAULT_MANIFEST, help='manifest path relative to the repository')
    parser.add_argument('--expected-total', type=int, default=None,
                        help='refuse to run unless the manifest holds exactly this many clips')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(compare(ROOT, args.engine, args.count, args.output, manifest=args.manifest,
                                 expected_total=args.expected_total))
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('{"status":"local_benchmark_failed","details":"withheld","cloud_enabled":false}')
        raise SystemExit(1) from None
