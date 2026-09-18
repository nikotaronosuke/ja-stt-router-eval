"""Bounded whole-turn replay on a local STT fault. Opt-in, testable without network.

When the primary (local) engine fails while a turn is in progress, the audio of
that turn, buffered up to `max_turn_s`, is replayed into the secondary (hosted)
engine from the start. Nothing is replayed unless the caller explicitly allowed
test audio to leave the machine, and live capture streams are refused outright.
"""
from __future__ import annotations

import asyncio
import math
import time

from .safety import safe_error


class FallbackEngine:
    name = 'parakeet_fallback'
    transmits_audio = True

    def __init__(self, primary, secondary, *, allow_cloud_test_audio=False, max_turn_s=31., emit=None):
        if not allow_cloud_test_audio:
            raise PermissionError('explicit fallback test audio approval required')
        if not math.isfinite(max_turn_s) or not 1 <= max_turn_s <= 31:
            raise ValueError('invalid bounded replay duration')
        self.primary = primary
        self.secondary = secondary
        self.max_turn_s = max_turn_s
        self.emit = emit or (lambda event: None)
        self.model = primary.model + ' -> ' + secondary.model
        self.metadata = {'fallback_policy': 'whole_current_turn_replay', 'max_turn_s': max_turn_s,
                         'frame_count_scope': 'canonical_16khz_input_before_secondary_resampling',
                         'primary_interval_s': primary.metadata.get('interval_s'),
                         'hints': secondary.metadata.get('hints'), 'delay': secondary.metadata.get('delay')}
        self.cloud_active = False
        self.primary_open_error = None
        self.turn_stats = []
        self.buffer = []

    async def open(self):
        try:
            await self.primary.open()
        except Exception as error:
            self.primary_open_error = safe_error(error)

    async def begin(self, callback):
        self.callback = callback
        self.buffer = []
        self.frames = 0
        self.stats = {'source_frames': 0, 'replayed_frames': 0, 'cloud_frames': 0, 'dropped_frames': 0,
                      'switched': False, 'first_cloud_ms': None}
        self.turn_stats.append(self.stats)

        def local_callback(text, when, final):
            if not self.cloud_active:
                callback(text, when, final)

        if self.cloud_active:
            self.secondary.budget.reserve(self.max_turn_s)
            await self.secondary.begin(self._cloud_callback)
        elif self.primary_open_error:
            await self._switch(self.primary_open_error)
        else:
            try:
                await self.primary.begin(local_callback)
            except Exception as error:
                await self._switch(safe_error(error))

    def _cloud_callback(self, text, when, final):
        if self.stats['first_cloud_ms'] is None and text:
            self.stats['first_cloud_ms'] = (when - self.stats.get('switch_started_ns', when)) / 1e6
        self.callback(text, when, final)

    async def _switch(self, reason):
        if self.cloud_active:
            raise RuntimeError('secondary_failed_no_implicit_retry')
        # Reserve a conservative full bounded turn before any network/credential access.
        self.secondary.budget.reserve(self.max_turn_s)
        self.cloud_active = True
        self.stats.update(switched=True, switch_started_ns=time.perf_counter_ns(), reason=reason)
        self.emit({'kind': 'status', 'engine': self.name, 'state': 'switching', 'reason': reason,
                   't_ns': self.stats['switch_started_ns']})
        cleanup = asyncio.create_task(self.primary.abort())
        try:
            await self.secondary.open()
            await self.secondary.begin(self._cloud_callback)
            self.stats['cloud_ready_ns'] = time.perf_counter_ns()
            for chunk in self.buffer:
                await self.secondary.feed(chunk)
                self.stats['replayed_frames'] += len(chunk.pcm16) // 2
                self.stats['cloud_frames'] += len(chunk.pcm16) // 2
            self.emit({'kind': 'status', 'engine': self.name, 'state': 'cloud_active',
                       't_ns': time.perf_counter_ns(), 'replayed_frames': self.stats['replayed_frames']})
        finally:
            await asyncio.gather(cleanup, return_exceptions=True)

    async def feed(self, chunk):
        if chunk.stream != 'fixture':
            raise PermissionError('fallback adapter currently permits test fixtures only')
        size = len(chunk.pcm16) // 2
        if chunk.rate != 16000 or len(chunk.pcm16) % 2 or self.frames + size > self.max_turn_s * 16000:
            raise ValueError('fallback_turn_buffer_limit')
        self.frames += size
        self.stats['source_frames'] = self.frames
        self.buffer.append(chunk)
        if self.cloud_active:
            await self.secondary.feed(chunk)
            self.stats['cloud_frames'] += size
        else:
            try:
                await self.primary.feed(chunk)
            except Exception as error:
                await self._switch(safe_error(error))

    async def finish(self):
        if self.cloud_active:
            await self.secondary.finish()
        else:
            try:
                await self.primary.finish()
            except Exception as error:
                await self._switch(safe_error(error))
                await self.secondary.finish()
        if self.cloud_active:
            self.stats['dropped_frames'] = max(0, self.frames - self.stats['cloud_frames'])
        else:
            self.stats['dropped_frames'] = 0
        self.buffer.clear()

    async def abort(self):
        await asyncio.gather(self.primary.abort(), self.secondary.abort(), return_exceptions=True)
        self.buffer.clear()

    async def close(self):
        await asyncio.gather(self.primary.close(), self.secondary.close(), return_exceptions=True)
        self.buffer.clear()
