"""Audio integrity audit or memory baseline; no transcript, audio or exception text is emitted.

    python scripts/diagnose.py audio --output artifacts/diagnose-audio.json
    python scripts/diagnose.py memory --output artifacts/diagnose-memory.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.diagnostics import audit_audio, memory_baseline, write_new  # noqa: E402

AUDIO_KEYS = ('groups', 'all_hashes_match', 'historical_channels_before_mono_available',
              'historical_meeting_device_match_verified')
MEMORY_KEYS = ('sample_count', 'stop_reason', 'physical_available_min_mib', 'commit_headroom_min_mib', 'load_allowed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['audio', 'memory'])
    parser.add_argument('--manifest', type=Path,
                        default=ROOT / 'fixtures' / 'manifests' / 'natural-speech-reviewed.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'audio':
        result = audit_audio(args.manifest)
        public = {key: result[key] for key in AUDIO_KEYS}
    else:
        result = memory_baseline()
        public = {key: result[key] for key in MEMORY_KEYS}
    write_new(args.output, result)
    print(json.dumps(public, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('{"status":"diagnostic_failed","detail":"withheld"}')
        raise SystemExit(1) from None
