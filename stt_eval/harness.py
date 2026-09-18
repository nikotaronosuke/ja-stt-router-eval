from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time

from .audio import AudioChunk, load_fixture, validate_manifest
from .metrics import TranscriptTrace, guard_reviewed_keyword_reference
from .report import write_report
from .resources import ResourceSampler
from .safety import DiagnosticError, safe_error


class Jsonl:
    def __init__(self, path: Path):
        self.stream = path.open('x', encoding='utf-8')
        self.lock = threading.Lock()

    def write(self, value):
        with self.lock:
            self.stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n')
            self.stream.flush()

    def close(self):
        with self.lock:
            self.stream.close()


class RunLock:
    """One audio/benchmark run per repository; the lock file records the owner PID."""
    def __init__(self, root):
        self.path = root / 'artifacts' / '.run.lock'

    def __enter__(self):
        self.path.parent.mkdir(exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._remove_if_stale()
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise DiagnosticError('run_lock_held') from None
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def _remove_if_stale(self):
        # Only a lock whose recorded owner process no longer exists is removed.
        # A live owner, an unreadable file or a failed process check keeps it,
        # and no process is terminated here. A worker left behind by a dead
        # owner ends on its own once its stdin pipe closes. Two starts racing
        # on the same stale file within microseconds are not guarded.
        try:
            owner = int(self.path.read_text(encoding='ascii').strip())
            import psutil
            alive = owner <= 0 or psutil.pid_exists(owner)
        except (OSError, ValueError, OverflowError, ImportError):
            raise DiagnosticError('run_lock_unreadable') from None
        if alive:
            raise DiagnosticError('run_lock_held')
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __exit__(self, *args):
        os.close(self.fd)
        self.path.unlink()


async def compare(manifest: Path, output: Path, engines: list, *, stop=None, emit=None,
                  duration_s=0., limit=None, repeats=1, chunk_ms=20, purpose='benchmark'):
    if (chunk_ms <= 0 or chunk_ms > 100 or not engines or not math.isfinite(duration_s)
            or duration_s < 0 or repeats < 1 or (limit is not None and limit < 1)):
        raise ValueError('invalid run settings')
    tests = validate_manifest(manifest)
    if limit:
        tests = tests[:limit]
    stop, emit = stop or threading.Event(), emit or (lambda e: None)
    output.mkdir(parents=True, exist_ok=False)
    cache = {t['test_id']: load_fixture(t['_audio_path']) for t in tests}
    if any(m['duration_s'] < .1 for _, m in cache.values()):
        raise ValueError('fixture must contain at least 100ms of audio')
    results_log, events_log, resources_log = [Jsonl(output / name) for name in
                    ['results.jsonl', 'events.jsonl', 'resources.jsonl']]
    sampler = ResourceSampler(resources_log.write).start()
    previous_emit = {engine: engine.emit for engine in engines}
    worker_samples = []

    def engine_event(event):
        if event['kind'] == 'resource':
            resources_log.write(event)
            worker_samples.append(event)
        elif event['kind'] in ['diagnostic', 'inference', 'status', 'maintenance']:
            events_log.write(event)
        emit(event)
    for engine in engines:
        engine.emit = engine_event
    start = time.perf_counter_ns()
    rows, trial = [], 0
    run = {'purpose': purpose, 'chunk_ms': chunk_ms, 'created_utc': datetime.now(timezone.utc).isoformat(),
           'requested_duration_s': duration_s, 'playback': 'realtime', 'mode': 'concurrent' if len(engines)>1 else 'isolated',
           'audio_kinds':sorted({test['audio_kind'] for test in tests}),
           'human_quality_evidence':all(test['audio_kind']=='human' and
               test.get('reference_review') in ['verified','dataset_reference'] for test in tests),
           'model_setup_errors': [], 'engines': {}, 'normalization': 'NFKC-casefold-no-punctuation-whitespace'}
    fatal = None
    opened = []
    try:
        for engine in engines:
            try:
                opening = asyncio.create_task(engine.open())
                try:
                    while not opening.done():
                        if stop.is_set():
                            opening.cancel()
                            raise asyncio.CancelledError()
                        await asyncio.wait([opening], timeout=.2)
                    await opening
                finally:
                    if not opening.done():
                        opening.cancel()
                        await asyncio.gather(opening, return_exceptions=True)
                opened.append(engine)
                run['engines'][engine.name] = {'model': engine.model, **engine.metadata}
            except Exception as error:
                run['model_setup_errors'].append({'engine': engine.name, 'error': safe_error(error)})
                raise
        run_start = time.perf_counter_ns()
        run['load_s'] = (run_start-start)/1e9
        cycle = 0
        while not stop.is_set():
            for test in tests:
                if stop.is_set() or (duration_s and (time.perf_counter_ns()-run_start)/1e9 >= duration_s):
                    break
                trial += 1
                pcm, audio_metadata = cache[test['test_id']]
                # One second of identical trailing silence is part of both inputs.
                pcm = pcm + bytes(32000)
                for engine in engines:
                    if engine.name == 'openai':
                        engine.budget.reserve(len(pcm)/32000)
                t0 = time.perf_counter_ns()
                traces = {engine: TranscriptTrace(t0) for engine in engines}
                queues = {engine: asyncio.Queue(maxsize=1600) for engine in engines}
                errors = {engine: [] for engine in engines}
                queue_max = {engine: 0. for engine in engines}
                emit({'kind': 'trial', 'trial': trial, 'test_id': test['test_id'], 'condition': test['condition']})

                async def consume(engine):
                    def callback(text, when, final):
                        traces[engine].add(text, when, final)
                        event = {'kind': 'transcript', 'trial': trial, 'engine': engine.name,
                                 't_ns': when, 'ms': (when-t0)/1e6, 'text': text, 'final': final}
                        events_log.write(event)
                        emit(event)
                    try:
                        await engine.begin(callback)
                        while True:
                            chunk = await queues[engine].get()
                            if chunk is None:
                                break
                            queue_max[engine] = max(queue_max[engine], (time.perf_counter_ns()-chunk.t_ns)/1e6)
                            await engine.feed(chunk)
                        await engine.finish()
                    except asyncio.CancelledError:
                        errors[engine].append('cancelled')
                        await engine.abort()
                        raise
                    except Exception as error:
                        errors[engine].append(safe_error(error))
                        await engine.abort()

                tasks = {engine: asyncio.create_task(consume(engine)) for engine in engines}
                chunk_bytes = int(16000*chunk_ms/1000)*2
                pacing_max_ms, sent_frames = 0., 0
                try:
                    for sequence, offset in enumerate(range(0, len(pcm), chunk_bytes)):
                        if stop.is_set():
                            break
                        due = t0 + round(offset/32000*1e9)
                        await asyncio.sleep(max(0, (due-time.perf_counter_ns())/1e9))
                        now = time.perf_counter_ns()
                        pacing_max_ms = max(pacing_max_ms, (now-due)/1e6)
                        chunk = AudioChunk(sequence, pcm[offset:offset+chunk_bytes], now)
                        for engine in engines:
                            if tasks[engine].done():
                                continue
                            try:
                                queues[engine].put_nowait(chunk)
                            except asyncio.QueueFull:
                                errors[engine].append('audio_queue_overflow')
                                tasks[engine].cancel()
                        sent_frames += len(chunk.pcm16)//2
                    # Last frame must also finish playing before commit.
                    await asyncio.sleep(max(0, (t0+round(sent_frames/16000*1e9)-time.perf_counter_ns())/1e9))
                    for engine in engines:
                        if not tasks[engine].done():
                            await queues[engine].put(None)
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                finally:
                    for task in tasks.values():
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                ended = time.perf_counter_ns()
                for engine in engines:
                    metrics = traces[engine].result(test['reference_text'], test['keywords'],
                                                   test['proper_nouns'], test['decision_keywords'])
                    reviewed = test.get('reference_review') in ['verified', 'dataset_reference']
                    if test['audio_kind'] == 'human' and not reviewed:
                        for key in ['cer', 'keyword_hit', 'keyword_recall', 'proper_noun_hits', 'proper_noun_accuracy']:
                            metrics[key] = None
                    guard_reviewed_keyword_reference(metrics, test)
                    if test.get('keyword_annotation'):
                        from .keyword_timing import keyword_end_metrics
                        metrics['keyword_end'] = keyword_end_metrics(
                            traces[engine].events, t0, test, test['keyword_annotation'],
                            audio_metadata['canonical_sha256'], audio_metadata['duration_s'])
                    if stop.is_set():
                        errors[engine].append('stopped')
                    if metrics['final_text'] is None and not errors[engine]:
                        errors[engine].append('missing_final')
                    row = {'trial': trial, 'engine': engine.name, 'model': engine.model,
                           'test_id': test['test_id'], 'condition': test['condition'],
                           'audio_kind': test['audio_kind'], 'reference_text': test['reference_text'],
                           'reference_review': test.get('reference_review','not_reviewed'),
                           'hints': engine.metadata.get('hints'), 't0_ns': t0,
                           **audio_metadata, **metrics, **sampler.summary(t0, ended),
                           'status': 'error' if errors[engine] else 'ok', 'errors': errors[engine],
                           'queue_max_ms': queue_max[engine], 'pacing_max_ms': pacing_max_ms,
                           'audio_frames': sent_frames, 'trailing_silence_s': 1.,
                           'last_partial_elapsed_s': max(((e['t_ns']-run_start)/1e9
                                for e in traces[engine].events if not e['final']), default=None),
                           'completed_elapsed_s': (ended-run_start)/1e9}
                    worker = [s for s in worker_samples if t0 <= s['t_ns'] <= ended]
                    for key in ['worker_cpu_pct', 'worker_ram_mib']:
                        values = [s[key] for s in worker if s.get(key) is not None]
                        row[key+'_avg'] = statistics.mean(values) if values else None
                        row[key+'_max'] = max(values) if values else None
                    results_log.write(row)
                    rows.append(row)
                if all(errors[e] for e in engines):
                    raise RuntimeError('all_engines_failed')
                # A dead local worker must not turn a long run into repeated timeout waits.
                if any(errors[e] for e in engines if e.name == 'parakeet'):
                    raise RuntimeError('local_worker_failed')
            cycle += 1
            if not duration_s and cycle >= repeats:
                break
            if duration_s and (time.perf_counter_ns()-run_start)/1e9 >= duration_s:
                break
        run['elapsed_s'] = (time.perf_counter_ns()-run_start)/1e9
    except BaseException as error:
        fatal = error
        run['fatal_error'] = safe_error(error)
        run.setdefault('elapsed_s', 0.)
    finally:
        for engine in engines:
            with contextlib.suppress(Exception):
                await engine.close()
            engine.emit = previous_emit[engine]
            if engine.name == 'openai':
                run.update(openai_connections=engine.connections, openai_rotations=engine.rotations,
                           openai_audio_s=engine.total_audio_s, openai_connection_s=engine.total_connection_s)
        sampler.stop()
        for logger in [results_log, events_log, resources_log]:
            logger.close()
        run['resources'] = sampler.summary()
        for key in ['worker_cpu_pct', 'worker_ram_mib', 'cuda_allocated_mib', 'cuda_reserved_mib']:
            values = [sample[key] for sample in worker_samples if sample.get(key) is not None]
            run['resources'][key + '_avg'] = statistics.mean(values) if values else None
            run['resources'][key + '_max'] = max(values) if values else None
        run['cancelled'] = stop.is_set()
        requested_soak = duration_s >= 3600
        latest_by_engine = {engine.name: max((r['last_partial_elapsed_s'] for r in rows
                           if r['engine']==engine.name and r.get('last_partial_elapsed_s') is not None and r['status']=='ok'), default=0)
                            for engine in engines}
        run['last_partial_turn_elapsed_s'] = latest_by_engine
        run['stability_status'] = ('PASS' if duration_s >= 1800 and not fatal and not stop.is_set() and
                                  run['elapsed_s'] >= duration_s and rows and
                                  all(r['status']=='ok' for r in rows) and
                                  all(v >= duration_s-35 for v in latest_by_engine.values())
                                  else 'FAIL' if duration_s >= 1800 else '未計測')
        run['soak_status'] = ('PASS' if requested_soak and not fatal and not stop.is_set() and
                              run['elapsed_s'] >= duration_s and all(v >= 3600 for v in latest_by_engine.values()) and
                              all(r['status']=='ok' for r in rows) else 'FAIL' if requested_soak else '未計測')
        run['trial_count'] = trial
        runtime_start = start + int(run.get('load_s', 0) * 1e9)
        runtime_samples = [sample for sample in sampler.samples if sample['t_ns'] >= runtime_start]
        run['memory_change'] = {}
        for key in ['app_ram_mib', 'system_ram_mib', 'vram_mib']:
            values = [sample[key] for sample in runtime_samples if sample.get(key) is not None]
            if len(values) >= 120:
                drift = statistics.median(values[-60:]) - statistics.median(values[:60])
            else:
                drift = None
            run['memory_change'][key + '_last60_minus_first60'] = drift
        (output / 'run.json').write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding='utf-8')
        write_report(output, rows, run)
    if fatal:
        raise fatal
    return run
