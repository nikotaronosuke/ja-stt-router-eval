"""Memory-gated local benchmark on a fixed manifest; only anonymous metrics leave this process.

The benchmark replays reviewed clips into one local engine while a monitor
watches physical memory and commit headroom every second. Loading is refused
when the pre-flight window shows too little headroom, and a run stops on acute
or persistent pressure. Sample collection runs on a thread so that it cannot
delay the audio pacing that the latency figures depend on; the maximum pacing
error is recorded next to the results.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path

from .audio import AudioChunk, load_fixture, validate_manifest
from .diagnostics import MemoryGate, memory_baseline, write_new
from .engines import ParakeetEngine
from .harness import RunLock
from .keyword_timing import keyword_end_metrics
from .memory_monitor import PagingCounters, application_memory
from .metrics import TranscriptTrace, edit_distance, hits, normalize, quantiles
from .resources import _windows_memory
from .sherpa_engine import SherpaEngine
from .spelling import aliases, equivalence_scores

DEFAULT_MANIFEST = 'fixtures/manifests/natural-speech-reviewed.json'
ENGINES = ('parakeet', 'sherpa')
MAX_CLIPS = 28
# Physical memory that must be free before the WSL/CUDA engine is loaded: the historical
# worker start-up peak plus the stop line, so a load cannot itself trigger the stop.
STARTUP_HEADROOM_MIB = 4500
PACING_LIMIT_MS = 50
REPLAY_INTERVAL_S = .25


def trial_metrics(test, events, t0, meta, ident):
    scores = TranscriptTrace(t0, events).result(test['reference_text'], test['keywords'], [], [])
    final = scores['final_text'] or ''
    timing = keyword_end_metrics(events, t0, test, test['keyword_annotation'], meta['canonical_sha256'],
                                 meta['duration_s'])
    latencies = [item['latency_ms'] for item in timing['items'] if item['review'] == 'human_verified']
    equivalent = equivalence_scores(test, final)['keyword_equivalent_hit']
    changes = []
    for term in test['keywords']:
        seen = [bool(hits(event['text'], [aliases(term)])[0]) for event in events]
        changes.append(any(a and not b for a, b in zip(seen, seen[1:])))
    reference = normalize(test['reference_text'])
    return {'clip': ident, 'cer': scores['cer'], 'reference_characters': len(reference),
            'edit_count': edit_distance(reference, normalize(final)),
            'keyword_equivalent_hit': equivalent, 'keyword_end_ms': latencies,
            'first_partial_ms': scores['first_partial_ms'],
            'transient_disappearance_count': sum(changes),
            'meaning_review': 'not_reviewed', 'final_normalized_matches_reference': normalize(final) == reference}


def aggregate(rows):
    values = [value for row in rows for value in row['keyword_end_ms']]
    flags = [value for row in rows for value in row['keyword_equivalent_hit']]
    chars = sum(row['reference_characters'] for row in rows)
    return {'clips': len(rows),
            'cer_micro': sum(row['edit_count'] for row in rows) / chars if chars else None,
            'cer_macro': sum(row['cer'] for row in rows) / len(rows) if rows else None,
            'keyword_detected': sum(flags), 'keyword_total': len(flags),
            'annotated': len(values), 'end_detected': sum(value is not None for value in values),
            'end_missing': sum(value is None for value in values),
            'within_500ms': sum(value is not None and value <= 500 for value in values),
            'negative': sum(value is not None and value < 0 for value in values),
            'keyword_end_ms': quantiles(values),
            'first_partial_ms': quantiles([row['first_partial_ms'] for row in rows]),
            'transient_disappearance_count': sum(row['transient_disappearance_count'] for row in rows),
            'meaning_review_complete': False}


async def compare(root: Path, engine_name: str, count: int, output: Path, manifest=None, expected_total=None):
    if engine_name not in ENGINES or not isinstance(count, int) or not 1 <= count <= MAX_CLIPS:
        raise ValueError('invalid_local_benchmark')
    if output.exists():
        raise ValueError('output_exists')
    # There is no cloud engine or fallback option in this execution path.
    tests = validate_manifest(root / (manifest or DEFAULT_MANIFEST))
    if expected_total is not None and len(tests) != expected_total:
        raise ValueError('fixed_dataset_changed')
    if len(tests) < count:
        raise ValueError('manifest_shorter_than_count')
    return await compare_inputs(root, engine_name, tests[:count], output)


def load_input(test):
    if '_pcm' not in test:
        return load_fixture(test['_audio_path'])
    pcm = test['_pcm']
    if not isinstance(pcm, bytes) or not 3200 <= len(pcm) <= 30 * 32000 or len(pcm) % 2:
        raise ValueError('invalid_memory_input')
    return pcm, {'canonical_sha256': hashlib.sha256(pcm).hexdigest(), 'duration_s': len(pcm) / 32000}


async def compare_inputs(root, engine_name, tests, output, cancel=None):
    if engine_name not in ENGINES or not 1 <= len(tests) <= MAX_CLIPS or output.exists():
        raise ValueError('invalid_local_inputs')
    preflight = await asyncio.to_thread(memory_baseline)
    if cancel is not None and cancel.is_set():
        return {'status': 'cancelled', 'cloud_enabled': False}
    if engine_name == 'parakeet' and (preflight.get('physical_available_min_mib') or 0) < STARTUP_HEADROOM_MIB:
        preflight['load_allowed'] = False
        preflight['stop_reason'] = 'historical_startup_peak_headroom_insufficient'
    if not preflight['load_allowed']:
        result = {'status': 'not_started_memory_gate', 'cloud_enabled': False, 'preflight': preflight}
        write_new(output, result)
        return {'status': result['status'], 'cloud_enabled': False}
    worker_samples = []

    def resource(event):
        if event.get('kind') == 'resource':
            keys = ('worker_ram_mib', 'cuda_allocated_mib', 'cuda_reserved_mib', 'wsl_available_mib',
                    'wsl_swap_used_mib')
            worker_samples.append({key: event[key] for key in keys if isinstance(event.get(key), (int, float))})

    if engine_name == 'parakeet':
        engine = ParakeetEngine(root, interval_s=REPLAY_INTERVAL_S, emit=resource)
    else:
        engine = SherpaEngine(root, interval_s=REPLAY_INTERVAL_S, emit=resource)
    if getattr(engine, 'transmits_audio', False):
        raise ValueError('cloud_must_be_disabled')
    rows = []
    samples = []
    stop = asyncio.Event()
    phase = 'startup'
    reason = None
    counters = None
    gate = MemoryGate()
    started = time.monotonic()
    sampling = None

    def collect_sample(sample_phase, stamp):
        collection_started = time.monotonic()
        memory, _ = _windows_memory()
        paging = counters.sample()
        committed, limit = paging.get('committed_bytes'), paging.get('commit_limit_bytes')
        headroom = (limit - committed) / 2**20 if committed is not None and limit is not None else None
        sample = {'phase': sample_phase, 'elapsed_s': stamp - started,
                  'physical_available_mib': memory['system_available_mib'],
                  'physical_used_mib': memory['system_ram_mib'],
                  'committed_mib': committed / 2**20 if committed is not None else None,
                  'commit_limit_mib': limit / 2**20 if limit is not None else None,
                  'commit_headroom_mib': headroom, 'host_rss_mib': memory['app_ram_mib'],
                  'applications': application_memory()}
        if engine_name == 'sherpa' and engine.process and engine.process.returncode is None:
            import psutil
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                parent = psutil.Process(engine.process.pid)
                # Keep launcher and model RSS separately; never sum shared pages.
                sample['worker_process_rss_mib'] = [process.memory_info().rss / 2**20
                                                    for process in [parent, *parent.children(recursive=True)]]
        sample['collection_ms'] = (time.monotonic() - collection_started) * 1000
        return sample

    async def monitor():
        nonlocal reason, sampling
        while True:
            stamp = time.monotonic()
            if cancel is not None and cancel.is_set():
                reason = 'operator_cancelled'
                stop.set()
                return
            try:
                sampling = asyncio.create_task(asyncio.to_thread(collect_sample, phase, stamp))
                sample = await asyncio.shield(sampling)
                reason = gate.observe(sample['physical_available_mib'], sample['commit_headroom_mib'], stamp)
                samples.append(sample)
            except Exception:
                reason = 'memory_counter_missing'
            if reason:
                stop.set()
                return
            await asyncio.sleep(max(0, 1 - (time.monotonic() - stamp)))

    async def guarded(awaitable):
        job = asyncio.create_task(awaitable)
        alarm = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([job, alarm], return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                raise RuntimeError('memory_stop')
            return await job
        finally:
            for task in (job, alarm):
                if not task.done():
                    task.cancel()
            await asyncio.gather(job, alarm, return_exceptions=True)

    async def replay(test, pcm, meta, ident):
        events = []

        def callback(text, when, final):
            events.append({'text': text, 't_ns': when, 'final': final})

        await engine.begin(callback)
        padded = pcm + bytes(32000)  # Same one-second tail as the replay baseline.
        t0 = time.perf_counter_ns()
        pacing_max_ms = 0.
        for sequence, offset in enumerate(range(0, len(padded), 640)):
            due = t0 + round(offset / 32000 * 1e9)
            await asyncio.sleep(max(0, (due - time.perf_counter_ns()) / 1e9))
            when = time.perf_counter_ns()
            pacing_max_ms = max(pacing_max_ms, (when - due) / 1e6)
            await engine.feed(AudioChunk(sequence, padded[offset:offset + 640], when))
        await asyncio.sleep(max(0, (t0 + round(len(padded) / 32000 * 1e9) - time.perf_counter_ns()) / 1e9))
        await engine.finish()
        return {**trial_metrics(test, events, t0, meta, ident), 'pacing_max_ms': pacing_max_ms,
                'pacing_valid': pacing_max_ms <= PACING_LIMIT_MS}

    status = 'ok'
    startup_s = None
    with RunLock(root):
        counters = PagingCounters()
        watching = asyncio.create_task(monitor())
        try:
            await guarded(engine.open())
            startup_s = time.monotonic() - started
            phase = 'warmup'
            warm, _ = load_input(tests[0])
            await guarded(engine._infer(warm))
            del warm
            phase = 'recognition'
            for index, test in enumerate(tests):
                pcm, meta = load_input(test)
                if meta['canonical_sha256'] != test['canonical_sha256']:
                    raise ValueError('input_changed')
                rows.append(await guarded(replay(test, pcm, meta, f'clip_{index + 1:02}')))
                del pcm
        except (Exception, asyncio.CancelledError):
            if reason == 'operator_cancelled':
                status = 'cancelled'
            elif stop.is_set():
                status = 'stopped_memory'
            else:
                status = 'local_failure'
        finally:
            phase = 'exit'
            await engine.close()
            if not stop.is_set():
                await asyncio.sleep(5)
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
            if sampling:
                await asyncio.gather(sampling, return_exceptions=True)
            counters.close()
    result = {'status': status, 'engine': engine_name, 'cloud_enabled': False, 'api_budget_usd': 0,
              'startup_s': startup_s, 'stop_reason': reason, 'preflight': preflight, 'host_samples': samples,
              'worker_samples': worker_samples, 'rows': rows, 'summary': aggregate(rows),
              'audio_or_transcripts_saved': False, 'coexistence': 'must_be_recorded_by_operator',
              'worker_exited': engine.process is None or engine.process.returncode is not None,
              'measurement_version': 2,
              'pacing_valid': bool(rows) and all(row['pacing_valid'] for row in rows)}
    write_new(output, result)
    return {key: result[key] for key in ('status', 'engine', 'cloud_enabled', 'startup_s', 'stop_reason',
                                         'summary', 'worker_exited')}
