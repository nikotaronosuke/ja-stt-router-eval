from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import math
import threading
import time

from .capture import WasapiCapture
from .resources import ResourceSampler


async def listen(engines, stop: threading.Event, emit, *, seconds=600, threshold=.004,
                 silence_s=.5, max_turn_s=20, allow_live_cloud=False):
    """Ephemeral live diagnostics: no WAV/transcript/event writer exists here."""
    if any(e.name == 'openai' or getattr(e,'transmits_audio',False) for e in engines) and not allow_live_cloud:
        raise PermissionError('separate live cloud approval required')
    limits = [threshold, silence_s, max_turn_s] + ([seconds] if seconds is not None else [])
    if any(not math.isfinite(value) or value <= 0 for value in limits):
        raise ValueError('invalid live settings')
    loop = asyncio.get_running_loop()
    chunks = asyncio.Queue(maxsize=150)
    overflow = threading.Event()
    def enqueue(chunk):
        try:
            chunks.put_nowait(chunk)
        except asyncio.QueueFull:
            overflow.set()
    def on_chunk(chunk):
        loop.call_soon_threadsafe(enqueue, chunk)
    capture = WasapiCapture(on_chunk=on_chunk, on_meter=emit)
    sampler = ResourceSampler(lambda sample: emit({'kind':'host_resource', **sample}))
    active, preroll, turn_start, quiet, turn_bytes = False, deque(maxlen=10), 0, 0., 0
    turns, last_partial_ns = 0, None
    last_chunk_received = None
    started = time.perf_counter_ns()
    try:
        for engine in engines:
            engine.emit = emit
            if engine.name == 'openai':
                engine.budget.reserve(seconds)
            opening = asyncio.create_task(engine.open())
            try:
                while not opening.done():
                    if stop.is_set():
                        opening.cancel()
                        raise asyncio.CancelledError()
                    await asyncio.wait([opening],timeout=.2)
                await opening
            finally:
                if not opening.done():
                    opening.cancel()
                    await asyncio.gather(opening,return_exceptions=True)
        capture.start()
        sampler.start()
        started = time.perf_counter_ns()
        while not stop.is_set() and (seconds is None or (time.perf_counter_ns()-started)/1e9 < seconds):
            if overflow.is_set() or capture.errors:
                raise RuntimeError('capture_backpressure')
            try:
                chunk = await asyncio.wait_for(chunks.get(), min(1.,silence_s))
            except asyncio.TimeoutError:
                # WASAPI may deliver no packets when all render clients stop.
                # End an active turn by wall time rather than waiting indefinitely.
                quiet_for = time.monotonic() - last_chunk_received if last_chunk_received is not None else 0.
                if active and last_chunk_received is not None and quiet_for >= silence_s:
                    await asyncio.gather(*(e.finish() for e in engines))
                    active = False
                continue
            last_chunk_received = time.monotonic()
            import numpy as np
            values = np.frombuffer(chunk.pcm16,dtype='<i2').astype(float)/32768
            rms = float(np.sqrt(np.mean(values*values))) if len(values) else 0.
            duration = len(chunk.pcm16)/32000
            if not active:
                preroll.append(chunk)
                if rms < threshold:
                    continue
                active, quiet, turn_bytes = True, 0., 0
                turn_start = preroll[0].t_ns
                turns += 1
                for engine in engines:
                    def callback(text, when, final, engine_name=engine.name):
                        nonlocal last_partial_ns
                        if not final:
                            last_partial_ns = when
                        emit({'kind':'transcript','engine':engine_name,'text':text,'final':final,
                              'ms':(when-turn_start)/1e6})
                    await engine.begin(callback)
                while preroll:
                    initial = preroll.popleft()
                    await asyncio.gather(*(e.feed(initial) for e in engines))
                    turn_bytes += len(initial.pcm16)
            else:
                await asyncio.gather(*(e.feed(chunk) for e in engines))
                turn_bytes += len(chunk.pcm16)
            quiet = quiet+duration if rms < threshold else 0.
            if quiet >= silence_s or turn_bytes/32000 >= max_turn_s:
                await asyncio.gather(*(e.finish() for e in engines))
                active = False
        if active:
            await asyncio.gather(*(e.finish() for e in engines))
    finally:
        async def release_resources():
            try:
                capture.stop()
            finally:
                try:
                    sampler.stop()
                finally:
                    for engine in engines:
                        with contextlib.suppress(Exception):
                            await engine.close()
        # A stop request may cancel listen while normal shutdown is already
        # awaiting the worker. Keep cleanup alive and wait for it before exit.
        cleanup = asyncio.create_task(release_resources())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
    return {'audio_saved':False, 'transcript_saved':False, 'turns':turns,
            'capture':capture.summary(), 'resources':sampler.summary(),
            'elapsed_s':(time.perf_counter_ns()-started)/1e9,
            'last_partial_elapsed_s':(last_partial_ns-started)/1e9 if last_partial_ns else None}
