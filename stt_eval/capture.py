"""WASAPI capture of the default render loopback and the default microphone.

Both streams are opened at their device rates and converted to canonical 16 kHz
mono with a causal resampler. Loopback audio is handed to `on_chunk`; microphone
samples are only measured (level, frame counts, drops) and then discarded.
Nothing on this path is written to disk.

WASAPI loopback may deliver no packets while no application is rendering. With
`keep_render_active`, a silent output stream is kept open so the loopback keeps
producing frames; this is a diagnostic setting for continuity measurements, and
it does not change volume or device settings.
"""
from __future__ import annotations

import queue
import threading
import time

from .audio import AudioChunk, StreamingResampler, encode_pcm16

STREAMS = ('loopback', 'microphone')
SIGNAL_RMS = .004


def _stream_stats():
    return {'frames': 0, 'callbacks': 0, 'status_flags': 0, 'queue_dropped_frames': 0, 'max_rms': 0.,
            'last_ns': None, 'first_ns': None, 'max_callback_gap_ms': 0., 'gaps_over_100ms': 0,
            'rms_above_004_frames': 0, 'max_queue_wait_ms': 0., 'input_clipped_samples': 0,
            'input_samples': 0, 'channel_square_sum': [], 'mono_square_sum': 0.}


class WasapiCapture:
    """Default render loopback + default WASAPI mic. No recording on this path."""

    def __init__(self, on_chunk=None, chunk_ms=20, on_meter=None, keep_render_active=False):
        self.on_chunk = on_chunk or (lambda chunk: None)
        self.on_meter = on_meter or (lambda meter: None)
        self.chunk_ms = chunk_ms
        self.queue = queue.Queue(maxsize=200)
        self.stop_event = threading.Event()
        self.streams = []
        self.stats = {stream: _stream_stats() for stream in STREAMS}
        self.errors = []
        self.manager = None
        self.worker = None
        self.started_ns = None
        self.keep_render_active = keep_render_active
        self.silent_renderer = None
        self.render_stats = {'callbacks': 0, 'status_flags': 0}

    def start(self):
        import pyaudiowpatch as pa
        self.manager = pa.PyAudio()
        try:
            host = self.manager.get_host_api_info_by_type(pa.paWASAPI)
            speaker = self.manager.get_device_info_by_index(host['defaultOutputDevice'])
            loopback = self.manager.get_wasapi_loopback_analogue_by_dict(speaker)
            microphone = self.manager.get_device_info_by_index(host['defaultInputDevice'])
            devices = {'loopback': loopback, 'microphone': microphone}
            self.formats = {name: {'rate': int(device['defaultSampleRate']),
                                   'channels': int(device['maxInputChannels']),
                                   'sample_format': 'float32', 'canonical_rate': 16000}
                            for name, device in devices.items()}
            if any(form['channels'] < 1 for form in self.formats.values()):
                raise RuntimeError('default audio input unavailable')
            self.started_ns = time.perf_counter_ns()
            self.worker = threading.Thread(target=self._consume, daemon=True)
            self.worker.start()
            if self.keep_render_active:
                self._start_silent_render(pa, speaker)
            for name, device in devices.items():
                form = self.formats[name]

                def callback(data, frames, timing, status, stream_name=name):
                    now = time.perf_counter_ns()
                    stats = self.stats[stream_name]
                    stats['frames'] += frames
                    stats['callbacks'] += 1
                    stats['status_flags'] |= status
                    if stats['last_ns'] is not None:
                        gap = (now - stats['last_ns']) / 1e6
                        stats['max_callback_gap_ms'] = max(stats['max_callback_gap_ms'], gap)
                        stats['gaps_over_100ms'] += gap > 100
                    else:
                        stats['first_ns'] = now
                    stats['last_ns'] = now
                    try:
                        self.queue.put_nowait((stream_name, data, frames, now))
                    except queue.Full:
                        stats['queue_dropped_frames'] += frames
                    return None, pa.paContinue

                stream = self.manager.open(format=pa.paFloat32, channels=form['channels'], rate=form['rate'],
                                           input=True, input_device_index=device['index'],
                                           frames_per_buffer=max(1, round(form['rate'] * self.chunk_ms / 1000)),
                                           stream_callback=callback, start=False)
                self.streams.append(stream)
            for stream in self.streams:
                stream.start_stream()
        except BaseException:
            self.stop()
            raise
        return self

    def _start_silent_render(self, pa, speaker):
        output_channels = int(speaker['maxOutputChannels'])
        output_rate = int(speaker['defaultSampleRate'])

        def render_silence(data, frames, timing, status):
            self.render_stats['callbacks'] += 1
            self.render_stats['status_flags'] |= status
            return bytes(frames * output_channels * 4), pa.paContinue

        self.silent_renderer = self.manager.open(format=pa.paFloat32, channels=output_channels, rate=output_rate,
                                                 output=True, output_device_index=speaker['index'],
                                                 frames_per_buffer=round(output_rate * self.chunk_ms / 1000),
                                                 stream_callback=render_silence)

    def _consume(self):
        import numpy as np
        resamplers = {name: StreamingResampler(form['rate'], 16000) for name, form in self.formats.items()}
        for name, resampler in resamplers.items():
            self.formats[name]['causal_filter_delay_ms'] = resampler.delay_ms
        sequence = {'loopback': 0, 'microphone': 0}
        try:
            # Blocks still queued when stop is requested are processed, not dropped.
            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    name, data, frames, when = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                channels = np.frombuffer(data, dtype='<f4').reshape(-1, self.formats[name]['channels'])
                if len(channels) != frames or not np.isfinite(channels).all():
                    raise ValueError('invalid_capture_samples')
                samples = channels.mean(axis=1)
                stats = self.stats[name]
                stats['max_queue_wait_ms'] = max(stats['max_queue_wait_ms'], (time.perf_counter_ns() - when) / 1e6)
                power = (channels.astype(np.float64) ** 2).sum(axis=0)
                if not stats['channel_square_sum']:
                    stats['channel_square_sum'] = [0.] * len(power)
                stats['channel_square_sum'] = [a + float(b) for a, b in zip(stats['channel_square_sum'], power)]
                stats['mono_square_sum'] += float(np.sum(samples.astype(np.float64) ** 2))
                stats['input_samples'] += channels.size
                stats['input_clipped_samples'] += int(np.count_nonzero(np.abs(channels) >= .999))
                rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.
                stats['max_rms'] = max(stats['max_rms'], rms)
                if rms > SIGNAL_RMS:
                    stats['rms_above_004_frames'] += frames
                self.on_meter({'kind': 'meter', 'stream': name, 'rms': rms})
                # Mic samples remain local and are discarded after measuring frames/level.
                if name == 'loopback':
                    pcm = encode_pcm16(resamplers[name].process(samples))
                    self.on_chunk(AudioChunk(sequence[name], pcm, when, name))
                    sequence[name] += 1
        except Exception as error:
            self.errors.append(type(error).__name__)
            self.stop_event.set()

    def stop(self):
        for stream in self.streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as error:
                self.errors.append(type(error).__name__)
        self.streams.clear()
        if self.silent_renderer:
            try:
                self.silent_renderer.stop_stream()
                self.silent_renderer.close()
            except Exception as error:
                self.errors.append(type(error).__name__)
            self.silent_renderer = None
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=3)
        if self.manager:
            self.manager.terminate()
            self.manager = None

    def summary(self):
        return {'capture': 'WASAPI_default_render_and_default_mic', 'audio_saved': False,
                'silent_render_active': self.keep_render_active, 'silent_render_stats': self.render_stats,
                'streams': self.stats, 'formats': getattr(self, 'formats', {}), 'errors': self.errors}
