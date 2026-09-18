"""Explicit local A/B audio-path diagnosis. Captured audio stays in bounded memory.

Five non-personal synthetic clips are played through the default output while
WASAPI loopback captures them (B). Each capture is aligned to its source (A) by
cross-correlation, then A and B are replayed into the local engine alternately
with the same interval, tail and scoring. The result says whether the local
playback-and-capture path degrades the waveform or the recognition; it is not a
live latency measurement and not evidence about a remote meeting route.

    python -m stt_eval.route_probe          # serves the consent page on 127.0.0.1:8767
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from .audio import load_fixture, validate_manifest
from .capture import WasapiCapture
from .diagnostics import wave_metrics, write_new
from .gated_benchmark import compare_inputs
from .recording_study import study_handler

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = 'fixtures/manifests/route-fixtures.json'
RESULT_DIR = 'artifacts/route-probe'
SOURCE_LABEL = 'nonpersonal_local_windows_tts'
CLIP_COUNT = 5


def result_paths(root):
    directory = root / RESULT_DIR
    primary = directory / 'routes-ab.json'
    capture = directory / 'routes-capture.json'
    if not primary.exists() and not capture.exists():
        return primary, capture
    # One retry only for a failed model startup; preserve the failed run.
    if primary.exists():
        old = json.loads(primary.read_text(encoding='utf-8'))
        failed_startup = old.get('status') == 'local_failure' and old.get('startup_s') is None and old.get('rows') == []
        if failed_startup:
            retry = directory / 'routes-ab-retry.json'
            retry_capture = directory / 'routes-capture-retry.json'
            if not retry.exists() and not retry_capture.exists():
                return retry, retry_capture
    raise ValueError('existing_result_preserved')


def aligned_capture(reference, received):
    """Align a known non-personal source for replay quality; not live latency truth."""
    import numpy as np
    from scipy.signal import correlate
    a = np.frombuffer(reference, dtype='<i2').astype(np.float64) / 32768
    b = np.frombuffer(received, dtype='<i2').astype(np.float64) / 32768
    if len(b) < len(a) or not np.any(a) or not np.any(b):
        raise ValueError('capture_missing')
    lag = int(np.argmax(correlate(b, a, mode='valid', method='fft')))
    matched = b[lag:lag + len(a)]
    correlation = float(np.dot(a, matched) / np.sqrt(np.dot(a, a) * np.dot(matched, matched)))
    from .audio import encode_pcm16
    return encode_pcm16(matched), {'alignment_offset_ms': lag / 16, 'waveform_correlation': correlation,
                                   'reference': wave_metrics(a[:, None], 16000),
                                   'captured': wave_metrics(matched[:, None], 16000)}


class RouteProbe:
    def __init__(self, root):
        self.root = root
        self.stop_event = threading.Event()
        self.recording = threading.Event()
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.phase = 'preparing'
        self.completed = 0
        self.error = None
        self.result = None
        self.pcm = None

    def stop(self):
        self.stop_event.set()

    def work(self):
        import winsound
        pairs = []
        diagnostics = []
        capture = None
        try:
            tests = validate_manifest(self.root / MANIFEST)
            if len(tests) != CLIP_COUNT or any(test.get('source') != SOURCE_LABEL for test in tests):
                raise ValueError('source_set_changed')
            output, capture_output = result_paths(self.root)
            for index, test in enumerate(tests):
                if self.stop_event.is_set():
                    return
                reference, meta = load_fixture(test['_audio_path'])
                blocks = []
                size = 0

                def consume(chunk):
                    nonlocal size
                    size += len(chunk.pcm16)
                    if size > 30 * 32000:
                        self.stop_event.set()
                        return
                    blocks.append(chunk.pcm16)

                capture = WasapiCapture(on_chunk=consume, keep_render_active=True)
                capture.start()
                self.recording.set()
                self.phase = 'capturing'
                if self.stop_event.wait(.5):
                    return
                winsound.PlaySound(str(test['_audio_path']), winsound.SND_FILENAME | winsound.SND_ASYNC)
                self.stop_event.wait(meta['duration_s'] + 1)
                winsound.PlaySound(None, 0)
                capture.stop()
                self.recording.clear()
                if self.stop_event.is_set():
                    return
                summary = capture.summary()
                capture = None
                dropped = any(stream['queue_dropped_frames'] or stream['status_flags']
                              for stream in summary['streams'].values())
                if summary['errors'] or dropped:
                    raise ValueError('capture_integrity_failed')
                matched, metrics = aligned_capture(reference, b''.join(blocks))
                diagnostics.append({'clip': f'route_{index + 1:02}', **metrics, 'capture': summary})
                # No private transcript, path or device name enters scoring reports.
                for pcm in (reference, matched):
                    pairs.append({'reference_text': test['reference_text'], 'keywords': test['keywords'],
                                  'proper_nouns': [], 'reference_review': 'verified', 'keyword_annotation': None,
                                  'source': 'nonpersonal_synthetic_route', '_pcm': pcm,
                                  'canonical_sha256': hashlib.sha256(pcm).hexdigest()})
                self.completed = index + 1
            if self.stop_event.is_set():
                return
            self.phase = 'scoring'
            self.result = asyncio.run(compare_inputs(self.root, 'parakeet', pairs, output, cancel=self.stop_event))
            if self.stop_event.is_set():
                return
            write_new(capture_output, {
                'rows': diagnostics, 'audio_saved': False, 'transcripts_saved': False, 'cloud_enabled': False,
                'comparison': 'A original / B locally captured, aligned then replayed; not live latency',
                'synthetic_reference': 'TTS input text; not human speech accuracy evidence'})
            self.phase = 'complete' if self.result.get('status') == 'ok' else 'incomplete'
        except Exception:
            self.error = 'local_route_diagnostic_failed'
            self.phase = 'error'
        finally:
            winsound.PlaySound(None, 0)
            if capture:
                capture.stop()
            self.recording.clear()
            pairs.clear()
            if self.stop_event.is_set():
                self.phase = 'cancelled'
                self.result = None


class RouteStudy:
    page_filename = 'route_probe.html'

    def __init__(self, root=ROOT):
        self.root = root
        self.live = None
        self.recorder = None
        self.pending = None
        self.lock = threading.Lock()

    def start(self, data):
        if data.get('consent_local_test_recording') is not True:
            raise ValueError('explicit_capture_consent_required')
        if self.recorder and self.recorder.thread.is_alive():
            raise ValueError('busy')
        if (self.root / 'artifacts' / '.run.lock').exists():
            raise ValueError('benchmark_busy')
        result_paths(self.root)
        self.recorder = RouteProbe(self.root)
        self.recorder.thread.start()

    def state(self):
        recorder = self.recorder
        return {'busy': bool(recorder and recorder.thread.is_alive()),
                'recording': bool(recorder and recorder.recording.is_set()),
                'phase': recorder.phase if recorder else 'idle',
                'completed': recorder.completed if recorder else 0,
                'error': recorder.error if recorder else None,
                'result': recorder.result if recorder else None,
                'audio_saved': False, 'cloud_enabled': False, 'pending': None, 'takes': []}


def serve(port=8767):
    study = RouteStudy()
    server = ThreadingHTTPServer(('127.0.0.1', port), study_handler(study, port))
    print(json.dumps({'state': 'ready', 'port': port, 'recording': False, 'cloud': False}), flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        if study.recorder:
            study.recorder.stop()
            study.recorder.thread.join(timeout=45)
        server.server_close()


if __name__ == '__main__':
    serve()
