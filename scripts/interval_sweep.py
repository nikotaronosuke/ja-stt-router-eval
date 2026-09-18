"""Local-only sequential prefix-interval trials that keep one loaded GPU worker.

    python scripts/interval_sweep.py --output artifacts/interval-sweep --minutes 30.5
    python scripts/interval_sweep.py --output artifacts/interval-remaining --intervals 300 250 200 \
        --extra-manifest fixtures/manifests/natural-speech-reviewed.json

Each interval is the wait for new audio after the previous inference finished
(see `ParakeetEngine`). The same model process is reused across intervals so the
comparison is not confounded by a reload. A file named STOP in the output
directory ends the sweep at the next trial boundary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.engines import ParakeetEngine  # noqa: E402
from stt_eval.harness import Jsonl, RunLock, compare  # noqa: E402
from stt_eval.resources import ResourceSampler  # noqa: E402
from stt_eval.safety import safe_error  # noqa: E402

INTERVALS_MS = (500, 300, 250, 200)
PROBE_MANIFEST = ROOT / 'fixtures' / 'manifests' / 'synthetic-smoke.json'
DRAFTS_DIR = ROOT / 'artifacts' / 'review-drafts'


class SharedWorker:
    """compare owns trial state; the sweep owns the already-open worker."""

    def __init__(self, engine):
        self.engine = engine
        self.name = engine.name
        self.model = engine.model
        self.metadata = engine.metadata
        self._emit = lambda event: None

    @property
    def emit(self):
        return self._emit

    @emit.setter
    def emit(self, value):
        self._emit = value
        self.engine.emit = value

    async def open(self):
        pass

    async def close(self):
        pass

    async def begin(self, callback):
        await self.engine.begin(callback)

    async def feed(self, chunk):
        await self.engine.feed(chunk)

    async def finish(self):
        await self.engine.finish()

    async def abort(self):
        await self.engine.abort()


async def probe_transcript(worker):
    """Recognize the first synthetic fixture; used to detect decoder-state changes."""
    from stt_eval.audio import load_fixture, validate_manifest
    fixture = validate_manifest(PROBE_MANIFEST)[0]
    pcm, _ = load_fixture(fixture['_audio_path'])
    text, _ = await worker._infer(pcm)
    return text


async def main(args):
    from stt_eval.metrics import normalize
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    progress = output / 'progress.json'
    stop = threading.Event()

    def write_progress(**data):
        temporary = progress.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(progress)

    async def watch_stop():
        while not stop.is_set():
            if (output / 'STOP').exists():
                stop.set()
            await asyncio.sleep(.5)

    watcher = asyncio.create_task(watch_stop())
    startup_log = Jsonl(output / 'startup-resources.jsonl')
    sampler = ResourceSampler(startup_log.write).start()

    def startup_event(event):
        if event['kind'] == 'resource':
            startup_log.write(event)

    worker = ParakeetEngine(ROOT, emit=startup_event)
    started = time.perf_counter()
    summary = {'intervals_ms': args.intervals, 'requested_each_s': args.minutes * 60,
               'shared_worker': True, 'api_audio_s': 0, 'runs': [],
               'dataset_scope': 'public read speech and synthetic fixtures; not natural-speech evidence'}
    try:
        write_progress(state='loading', completed=0, total=len(args.intervals))
        await worker.open()
        worker.emit = lambda event: None
        summary['load_s'] = time.perf_counter() - started
        summary['worker_metadata'] = dict(worker.metadata)
        sampler.stop()
        startup_log.close()
        if args.draft_manifest:
            from stt_eval.review_drafts import generate_drafts
            before = await probe_transcript(worker)
            write_progress(state='annotation_drafts', completed=0, total=len(args.intervals))

            def draft_progress(count):
                write_progress(state='annotation_drafts', drafts=count, completed=0, total=len(args.intervals))

            summary['annotation_drafts_generated'] = await generate_drafts(
                worker, args.draft_manifest.resolve(), DRAFTS_DIR, draft_progress)
            after = await probe_transcript(worker)
            summary['post_annotation_probe_equal'] = normalize(before) == normalize(after)
            if not summary['post_annotation_probe_equal']:
                raise RuntimeError('annotation_changed_decoder_output')
        shared = SharedWorker(worker)
        for index, interval in enumerate(args.intervals):
            if stop.is_set():
                break
            worker.interval = interval / 1000
            worker.metadata['interval_s'] = interval / 1000
            if args.extra_manifest:
                write_progress(state='natural_fixture_comparison', interval_ms=interval,
                               completed=index, total=len(args.intervals))
                await compare(args.extra_manifest.resolve(), output / f'natural-{interval}ms', [shared],
                              stop=stop, purpose='same_natural_audio_interval_comparison')
            if interval in args.skip_stability:
                summary['runs'].append({'interval_ms': interval, 'status': 'PREVIOUS_RUN',
                                        'note': 'stability evidence remains in the earlier baseline output; '
                                                'not counted as a new PASS'})
                continue

            def emit(event, interval=interval, index=index):
                if event['kind'] == 'trial':
                    write_progress(state='running', interval_ms=interval, trial=event['trial'],
                                   completed=index, total=len(args.intervals))

            result = await compare(args.manifest.resolve(), output / f'interval-{interval}ms', [shared],
                                   duration_s=args.minutes * 60, stop=stop, emit=emit,
                                   purpose='interval_stability')
            summary['runs'].append({'interval_ms': interval, 'trial_count': result['trial_count'],
                                    'elapsed_s': result['elapsed_s'], 'status': result['stability_status']})
            if result['stability_status'] != 'PASS' and args.minutes >= 30:
                break
        complete = len(summary['runs']) == len(args.intervals)
        all_passed = all(run['status'] in ['PASS', 'PREVIOUS_RUN'] for run in summary['runs'])
        summary['status'] = 'PASS' if complete and all_passed else 'INCOMPLETE'
        if args.skip_stability and summary['status'] == 'PASS':
            summary['status'] = 'COMPLETE_WITH_EXTERNAL_BASELINE'
        if args.probe_cache_after and not stop.is_set():
            cache_log = Jsonl(output / 'cache-probe-resources.jsonl')
            cache_sampler = ResourceSampler(cache_log.write).start()

            def cache_event(event):
                if event['kind'] in ['resource', 'maintenance', 'diagnostic', 'inference']:
                    cache_log.write(event)

            worker.emit = cache_event
            try:
                before = await probe_transcript(worker)
                await worker.release_unused_cache()
                after = await probe_transcript(worker)
                equal = normalize(before) == normalize(after)
                summary['cache_probe'] = {'status': 'PASS' if equal else 'OUTPUT_CHANGED',
                                          'same_audio': True, 'normalized_transcript_equal': equal}
            except Exception as error:
                summary['cache_probe'] = {'status': 'FAIL', 'error': safe_error(error),
                                          'note': 'Independent experiment after completed interval runs; '
                                                  'not enabled in normal capture.'}
            finally:
                worker.emit = lambda event: None
                cache_sampler.stop()
                cache_log.close()
    except BaseException as error:
        summary['status'] = 'FAIL'
        summary['error'] = safe_error(error)
        raise
    finally:
        stop.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        worker.emit = lambda event: None
        await worker.close()
        sampler.stop()
        if not startup_log.stream.closed:
            startup_log.close()
        (output / 'sweep.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        write_progress(state=summary.get('status', 'FAIL'), completed=len(summary['runs']),
                       total=len(args.intervals))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sequential prefix-interval stability trials')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'fixtures' / 'manifests' / 'comparison-160.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--minutes', type=float, default=30.5)
    parser.add_argument('--intervals', nargs='+', type=int, default=list(INTERVALS_MS))
    parser.add_argument('--extra-manifest', type=Path)
    parser.add_argument('--draft-manifest', type=Path)
    parser.add_argument('--probe-cache-after', action='store_true')
    parser.add_argument('--skip-stability', nargs='*', type=int, default=[])
    arguments = parser.parse_args()
    if not 0 < arguments.minutes <= 65 or any(value not in INTERVALS_MS for value in arguments.intervals):
        parser.error('invalid sweep settings')
    with RunLock(ROOT):
        asyncio.run(main(arguments))
