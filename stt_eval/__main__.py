from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import threading
import time

from .safety import safe_error

ROOT = Path(__file__).resolve().parent.parent


def make_engines(args, emit=None):
    from .engines import OpenAIEngine, ParakeetEngine
    from .safety import AudioBudget
    engines = []
    if args.engine in ['both', 'openai']:
        engines.append(OpenAIEngine(AudioBudget(ROOT/'artifacts/budget-ledger.json', args.budget_usd),
                    allow_network=args.allow_cloud_fixtures, hints=args.hints,
                    delay=args.delay, rotation_s=args.rotation_seconds, emit=emit))
    if args.engine in ['both', 'parakeet']:
        engines.append(ParakeetEngine(ROOT, interval_s=args.parakeet_interval,
                                     precision=args.precision, emit=emit))
    return engines


def main():
    parser = argparse.ArgumentParser(prog='python -m stt_eval', description='Japanese STT comparison harness')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--manifest', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--engine', choices=['both', 'openai', 'parakeet'], default='both')
    run.add_argument('--allow-cloud-fixtures', action='store_true')
    run.add_argument('--budget-usd', type=float, default=5.)
    run.add_argument('--hints', action='store_true')
    run.add_argument('--delay', choices=['minimal','low','medium','high','xhigh'], default='medium')
    run.add_argument('--rotation-seconds', type=float, default=3300)
    run.add_argument('--parakeet-interval', type=float, default=.5)
    run.add_argument('--precision', choices=['fp32','fp16','bf16'], default='fp32')
    run.add_argument('--duration-minutes', type=float, default=0)
    run.add_argument('--limit', type=int)
    run.add_argument('--repeats', type=int, default=1)
    capture = sub.add_parser('capture-check')
    capture.add_argument('--seconds', type=float, default=10)
    capture.add_argument('--play-fixture', type=Path)
    capture.add_argument('--keep-render-active',action='store_true')
    capture.add_argument('--output', type=Path, required=True)
    sub.add_parser('ui')
    args = parser.parse_args()
    try:
        if args.command == 'run':
            from .harness import RunLock, compare
            if not math.isfinite(args.duration_minutes) or args.duration_minutes < 0 or args.repeats < 1 or (args.limit is not None and args.limit < 1):
                parser.error('positive limits and nonnegative duration required')
            with RunLock(ROOT):
                result = asyncio.run(compare(args.manifest.resolve(), args.output.resolve(), make_engines(args),
                         duration_s=args.duration_minutes*60, limit=args.limit, repeats=args.repeats))
            print(json.dumps({'trials': result['trial_count'], 'soak_status': result['soak_status']}))
        elif args.command == 'capture-check':
            from .capture import WasapiCapture
            from .resources import ResourceSampler
            if not 0 < args.seconds <= 3900:
                parser.error('capture seconds must be 0..3900')
            if args.output.exists():
                parser.error('output already exists')
            sampler, capturer = ResourceSampler().start(), WasapiCapture(keep_render_active=args.keep_render_active)
            try:
                capturer.start()
                if args.play_fixture:
                    import winsound
                    winsound.PlaySound(str(args.play_fixture.resolve()), winsound.SND_FILENAME | winsound.SND_ASYNC)
                threading.Event().wait(args.seconds)
            finally:
                capturer.stop()
                sampler.stop()
            summary = {**capturer.summary(), 'resource_summary':sampler.summary(),
                       'duration_s':args.seconds}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(summary, indent=2), encoding='utf-8')
            print(json.dumps({'capture_errors':len(summary['errors']),
                              'loopback_frames':summary['streams']['loopback']['frames'],
                              'microphone_frames':summary['streams']['microphone']['frames']}))
        else:
            from .ui import main as ui_main
            ui_main()
    except KeyboardInterrupt:
        print('stopped')
        return 130
    except Exception as error:
        print(json.dumps({'error':safe_error(error)}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
