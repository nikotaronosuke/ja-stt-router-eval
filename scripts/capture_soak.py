"""Simultaneous mic/loopback continuity for about an hour; no saved audio or transcript.

    python scripts/capture_soak.py --minutes 61 --output artifacts/capture-soak

Frame counts, callback gaps, driver status flags, queue drops, paging counters and
coexisting application memory are sampled once per second. A file named STOP in the
output directory ends the run early.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.capture import WasapiCapture  # noqa: E402
from stt_eval.memory_monitor import PagingCounters, application_memory  # noqa: E402
from stt_eval.resources import ResourceSampler  # noqa: E402

COVERAGE_TOLERANCE = .002


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--minutes', type=float, default=61)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.minutes <= 180:
        parser.error('invalid duration')
    args.output.mkdir(parents=True, exist_ok=False)
    capturer = WasapiCapture(keep_render_active=True)
    monitor = ResourceSampler()
    paging = PagingCounters()
    started = time.perf_counter()
    summary = {'requested_s': args.minutes * 60, 'audio_saved': False, 'status': 'FAIL'}
    try:
        capturer.start()
        started = time.perf_counter()
        with (args.output / 'samples.jsonl').open('x', encoding='utf-8') as log:
            index = 0
            while time.perf_counter() - started < args.minutes * 60 and not (args.output / 'STOP').exists():
                now = time.perf_counter()
                row = {'t_ns': time.perf_counter_ns(), 'elapsed_s': now - started,
                       'paging': paging.sample(), 'streams': capturer.summary()['streams']}
                if index % 5 == 0:
                    row['apps'] = application_memory()
                    row['resources'] = monitor.sample()
                log.write(json.dumps(row) + '\n')
                log.flush()
                if index % 10 == 0:
                    progress = {'elapsed_s': now - started, 'errors': len(capturer.errors),
                                'mic_signal_detected': capturer.stats['microphone']['max_rms'] > .004}
                    (args.output / 'progress.json').write_text(json.dumps(progress), encoding='utf-8')
                index += 1
                threading.Event().wait(max(0, 1 - (time.perf_counter() - now)))
    finally:
        capturer.stop()
        elapsed = time.perf_counter() - started
        paging.close()
        capture = capturer.summary()
        coverage = {name: stream['frames'] / capture['formats'][name]['rate'] / elapsed
                    for name, stream in capture['streams'].items()}
        clean = not capture['errors'] and all(stream['status_flags'] == 0 and stream['queue_dropped_frames'] == 0
                                              for stream in capture['streams'].values())
        within = all(1 - COVERAGE_TOLERANCE <= value <= 1 + COVERAGE_TOLERANCE for value in coverage.values())
        summary.update(elapsed_s=elapsed, capture=capture, coverage=coverage,
                       actual_speech_validation='pending_human_recording_review',
                       status='PASS' if elapsed >= 3600 and clean and within else 'FAIL')
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps({'status': summary['status'], 'elapsed_s': elapsed, 'coverage': coverage}))


if __name__ == '__main__':
    main()
