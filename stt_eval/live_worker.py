"""One local GPU worker for explicitly recorded meeting test clips.

The recording page feeds loopback chunks to this worker while a clip is being
recorded, so the same audio that is saved as a fixture is also recognized live.
The worker keeps the receipt time of every captured block, which is what the
keyword-end latency of a live clip is measured against.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import threading
import time

from .engines import ParakeetEngine
from .harness import Jsonl, RunLock
from .metrics import TranscriptTrace
from .resources import ResourceSampler
from .safety import safe_error

PROBE_MANIFEST = 'fixtures/manifests/synthetic-smoke.json'
DRAFT_MANIFEST = 'fixtures/manifests/natural-speech.json'
DRAFTS_DIR = 'artifacts/review-drafts'
RESOURCE_KINDS = ('resource', 'maintenance', 'diagnostic', 'inference')


class LiveStudyWorker:
    def __init__(self, root, output, interval_s, probe_cache=False):
        self.root = root.resolve()
        self.output = output.resolve()
        self.interval = interval_s
        self.probe_cache = probe_cache
        self.commands = queue.Queue(maxsize=1800)
        self.stop_event = threading.Event()
        self.ready = False
        self.state = 'loading'
        self.error = None
        self.result = None
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def put(self, kind, value=None):
        try:
            self.commands.put_nowait((kind, value))
        except queue.Full:
            self.error = 'live_study_queue_overflow'
            raise RuntimeError('live_study_queue_overflow')

    def begin(self):
        if not self.ready or self.state not in ['ready', 'complete']:
            raise ValueError('local_model_not_ready')
        self.state = 'recording'
        self.result = None
        self.put('begin')

    def feed(self, chunk):
        self.put('chunk', chunk)

    def finish(self):
        self.put('finish')

    def prepare_drafts(self):
        if not self.ready or self.state not in ['ready', 'complete']:
            raise ValueError('local_model_not_idle')
        self.state = 'drafting'
        self.put('drafts')

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=15)
        if not self.thread.is_alive():
            self.ready = False
            self.state = 'off'

    def work(self):
        try:
            with RunLock(self.root):
                asyncio.run(self.run())
        except BaseException as error:
            self.error = safe_error(error)
            self.state = 'error'
            self.ready = False

    async def run(self):
        self.output.mkdir(parents=True, exist_ok=False)
        log = Jsonl(self.output / 'resources.jsonl')
        # Full samples are streamed to the local diagnostic file; no unbounded copy.
        sampler = ResourceSampler(log.write, max_samples=600).start()

        def engine_event(event):
            if event['kind'] in RESOURCE_KINDS:
                log.write(event)

        engine = ParakeetEngine(self.root, interval_s=self.interval, emit=engine_event)
        try:
            opening = asyncio.create_task(engine.open())
            while not opening.done():
                if self.stop_event.is_set():
                    opening.cancel()
                    await asyncio.gather(opening, return_exceptions=True)
                    return
                await asyncio.wait([opening], timeout=.2)
            await opening
            (self.output / 'worker-ready.json').write_text(json.dumps(engine.metadata), encoding='utf-8')
            # Compare the same existing synthetic fixture before/after idle cache release.
            from .audio import load_fixture, validate_manifest
            from .metrics import normalize
            fixture = validate_manifest(self.root / PROBE_MANIFEST)[0]
            pcm, _ = load_fixture(fixture['_audio_path'])
            before, _ = await engine._infer(pcm)
            if self.probe_cache:
                await engine.release_unused_cache()
                after, _ = await engine._infer(pcm)
                (self.output / 'cache-probe.json').write_text(json.dumps({
                    'same_audio': True, 'normalized_transcript_equal': normalize(before) == normalize(after),
                    'evidence': 'maintenance before/after counters in resources.jsonl',
                    'method': 'gc.collect plus torch.cuda.empty_cache while idle'}), encoding='utf-8')
            self.ready = True
            self.state = 'ready'
            active = False
            events = []
            timeline = []
            frames = 0
            queue_max = 0.
            digest = hashlib.sha256()
            while not self.stop_event.is_set():
                try:
                    kind, value = self.commands.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(.005)
                    continue
                if kind == 'quit':
                    break
                if kind == 'begin':
                    events = []
                    timeline = []
                    frames = 0
                    queue_max = 0.
                    digest = hashlib.sha256()
                    active = True

                    def callback(text, when, final):
                        events.append({'text': text, 't_ns': when, 'final': final})

                    await engine.begin(callback)
                elif kind == 'chunk' and active:
                    # Capture callback timestamp denotes receipt of the complete block.
                    timeline.append({'start_frame': frames, 'end_frame': frames + len(value.pcm16) // 2,
                                     'block_received_ns': value.t_ns})
                    queue_max = max(queue_max, (time.perf_counter_ns() - value.t_ns) / 1e6)
                    frames += len(value.pcm16) // 2
                    digest.update(value.pcm16)
                    await engine.feed(value)
                elif kind == 'finish' and active:
                    await engine.finish()
                    active = False
                    if timeline:
                        t0 = timeline[0]['block_received_ns'] - round(timeline[0]['end_frame'] / 16000 * 1e9)
                    else:
                        t0 = None
                    trace = TranscriptTrace(t0, events) if t0 is not None else None
                    metrics = trace.result('', [], [], []) if trace else {}
                    self.result = {'t0_ns': t0, 'events': events, 'timeline': timeline, 'metrics': metrics,
                                   'canonical_sha256': digest.hexdigest(), 'frames': frames,
                                   'queue_max_ms': queue_max, 'interval_s': self.interval,
                                   'path': 'meeting_app_to_WASAPI_to_local_stt_live',
                                   'clock_scope': 'receiver_host; excludes remote microphone/network transport latency'}
                    self.state = 'complete'
                elif kind == 'drafts' and not active:
                    from .review_drafts import generate_drafts
                    count = await generate_drafts(engine, self.root / DRAFT_MANIFEST, self.root / DRAFTS_DIR)
                    after, _ = await engine._infer(pcm)
                    probe_equal = normalize(before) == normalize(after)
                    (self.output / 'annotation-probe.json').write_text(json.dumps({
                        'generated': count, 'post_annotation_transcript_equal': probe_equal}), encoding='utf-8')
                    if not probe_equal:
                        raise RuntimeError('annotation_changed_decoder_output')
                    self.state = 'ready'
        finally:
            self.ready = False
            await engine.close()
            sampler.stop()
            log.close()
