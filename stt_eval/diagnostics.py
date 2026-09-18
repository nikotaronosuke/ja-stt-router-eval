"""Bounded, local-only diagnostics. Public output contains allowlisted aggregates."""
from __future__ import annotations

import json
import math
import statistics
import time
import wave
from pathlib import Path


class MemoryGate:
    """One-second samples; stop on missing data, acute pressure, or five seconds low."""
    def __init__(self):
        self.low_since = None
        self.previous = None

    def observe(self, physical_mib, commit_mib, now):
        values = (physical_mib, commit_mib)
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            return 'memory_counter_missing'
        if physical_mib < 512 or commit_mib < 256:
            return 'memory_acute_pressure'
        if self.previous is not None:
            before, previous_time = self.previous
            if now-previous_time <= 1.5 and any(a-b >= 1024 and b < 2048 for a,b in zip(before,values)):
                return 'memory_rapid_drop'
        self.previous = (values, now)
        if physical_mib < 1024 or commit_mib < 512:
            if self.low_since is None:
                self.low_since = now
            if now-self.low_since >= 5:
                return 'memory_persistent_pressure'
        else:
            self.low_since = None
        return None


def memory_baseline(seconds=30):
    from .memory_monitor import PagingCounters, application_memory
    from .resources import _windows_memory
    if not 1 <= seconds <= 30:
        raise ValueError('invalid_sample_bound')
    counters = PagingCounters()
    gate, rows, stop = MemoryGate(), [], None
    try:
        for _ in range(seconds):
            start = time.monotonic()
            memory, _ = _windows_memory()
            paging = counters.sample()
            committed, limit = paging.get('committed_bytes'), paging.get('commit_limit_bytes')
            headroom = (limit-committed)/2**20 if committed is not None and limit is not None else None
            stop = gate.observe(memory['system_available_mib'], headroom, start)
            rows.append({'physical_available_mib': memory['system_available_mib'],
                         'physical_used_mib': memory['system_ram_mib'],
                         'committed_mib': committed/2**20 if committed is not None else None,
                         'commit_limit_mib': limit/2**20 if limit is not None else None,
                         'commit_headroom_mib': headroom,
                         'diagnostic_process_rss_mib': memory['app_ram_mib'],
                         'applications': application_memory()})
            if stop:
                break
            if len(rows) < seconds:
                time.sleep(max(0, 1-(time.monotonic()-start)))
    finally:
        counters.close()
    def minimum(key):
        values = [r[key] for r in rows if r[key] is not None]
        return min(values) if len(values)==len(rows) and values else None
    physical, commit = minimum('physical_available_mib'), minimum('commit_headroom_mib')
    return {'samples': rows, 'sample_count': len(rows), 'stop_reason': stop,
            'physical_available_min_mib': physical, 'commit_headroom_min_mib': commit,
            'load_allowed': not stop and len(rows)==30 and physical >= 2048 and commit >= 2048,
            'model_loaded_by_diagnostic': False, 'recording_started': False,
            'video_condition': 'not_verified'}


def wave_metrics(samples, rate):
    import numpy as np
    from scipy.signal import welch
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim != 2 or not len(x) or not np.isfinite(x).all():
        raise ValueError('invalid_waveform')
    mono = x.mean(axis=1)
    power = np.mean(x*x, axis=0)
    mean_channel_power = float(power.mean())
    frequencies, spectrum = welch(mono, fs=rate, nperseg=min(1024,len(mono)))
    total = float(spectrum.sum())
    block = max(1, round(rate*.02))
    levels = [float(np.sqrt(np.mean(mono[i:i+block]**2))) for i in range(0,len(mono),block)]
    # Signal threshold describes level only, not VAD truth or intelligibility.
    quiet = [v < .004 for v in levels]
    leading = next((i for i,v in enumerate(quiet) if not v),len(quiet))*.02
    trailing = next((i for i,v in enumerate(reversed(quiet)) if not v),len(quiet))*.02
    return {'channel_rms': np.sqrt(power).tolist(), 'mono_rms': float(np.sqrt(np.mean(mono*mono))),
            'peak': float(np.max(np.abs(x))), 'clipped_fraction': float(np.mean(np.abs(x)>=.999)),
            'mono_power_ratio': float(np.mean(mono*mono))/mean_channel_power if mean_channel_power else None,
            'quiet_20ms_fraction': sum(quiet)/len(quiet), 'leading_quiet_s': leading,
            'trailing_quiet_s': trailing,
            'energy_above_4khz_fraction': float(spectrum[frequencies>4000].sum())/total if total else None}


def audit_audio(manifest: Path):
    import numpy as np
    from .audio import validate_manifest, load_fixture
    tests = validate_manifest(manifest)
    rows = []
    for index, test in enumerate(tests):
        with wave.open(str(test['_audio_path']), 'rb') as reader:
            rate, channels, width = reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
            raw = reader.readframes(reader.getnframes())
        if width != 2:
            raise ValueError('unsupported_wav_format')
        _, meta = load_fixture(test['_audio_path'])
        samples = np.frombuffer(raw, dtype='<i2').reshape(-1,channels)/32768.
        route = test.get('route')
        if route not in ('direct_mic','meet_loopback','zoom_loopback'):
            route = 'other'
        rows.append({'clip': f'clip_{index+1:02}', 'route': route, 'rate': rate,
                     'channels': channels, 'sample_width_bytes': width,
                     'duration_s': meta['duration_s'],
                     'canonical_hash_matches': meta['canonical_sha256']==test.get('canonical_sha256'),
                     **wave_metrics(samples,rate)})
    groups = {}
    for route in sorted({r['route'] for r in rows}):
        selected = [r for r in rows if r['route']==route]
        groups[route] = {'count': len(selected)}
        for key in ('mono_rms','peak','clipped_fraction','quiet_20ms_fraction','energy_above_4khz_fraction'):
            values = [r[key] for r in selected if r[key] is not None]
            groups[route][key] = {'min':min(values),'median':statistics.median(values),'max':max(values)} if values else None
    return {'rows': rows, 'groups': groups, 'all_hashes_match': all(r['canonical_hash_matches'] for r in rows),
            'historical_channels_before_mono_available': False,
            'historical_meeting_device_match_verified': False}


def write_new(path: Path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2,allow_nan=False)
