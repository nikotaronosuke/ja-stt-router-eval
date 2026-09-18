from __future__ import annotations

import ctypes
import os
import statistics
import subprocess
import threading
import time
from collections import deque


def _windows_memory():
    from ctypes import wintypes as w
    class Memory(ctypes.Structure):
        _fields_ = [('length', w.DWORD), ('load', w.DWORD)] + [
            (n, ctypes.c_ulonglong) for n in ['total', 'avail', 'page_total', 'page_avail', 'virtual_total', 'virtual_avail', 'extended']]
    class ProcessMemory(ctypes.Structure):
        _fields_ = [('cb', w.DWORD), ('faults', w.DWORD)] + [
            (n, ctypes.c_size_t) for n in ['peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'page', 'peak_page']]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(ProcessMemory), w.DWORD]
    memory, proc = Memory(), ProcessMemory()
    memory.length, proc.cb = ctypes.sizeof(memory), ctypes.sizeof(proc)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError('memory counter unavailable')
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(proc), proc.cb):
        raise OSError('process memory counter unavailable')
    times = [w.FILETIME() for _ in range(3)]
    if not kernel.GetSystemTimes(*(ctypes.byref(t) for t in times)):
        raise OSError('CPU counter unavailable')
    counters = [(t.dwHighDateTime << 32) + t.dwLowDateTime for t in times]
    return {'app_ram_mib': proc.ws / 2**20, 'system_ram_mib': (memory.total-memory.avail) / 2**20,
            'system_available_mib': memory.avail / 2**20,
            'system_commit_limit_mib': memory.page_total / 2**20,
            'system_commit_available_mib': memory.page_avail / 2**20,
            'app_page_faults': proc.faults}, counters


class ResourceSampler:
    def __init__(self, emit=None, interval_s=1., max_samples=None):
        self.samples = [] if max_samples is None else deque(maxlen=max_samples)
        self.emit = emit or (lambda sample: None)
        self.interval = interval_s
        self.stop_event = threading.Event()
        self.thread = None
        self.previous = None
        self.previous_system = None

    def sample(self):
        now, cpu = time.perf_counter_ns(), time.process_time()
        data = {'t_ns': now, 'app_cpu_pct': None, 'system_cpu_pct': None,
                'app_ram_mib': None, 'system_ram_mib': None, 'system_available_mib': None,
                'gpu_pct': None, 'vram_mib': None, 'gpu_temp_c': None, 'counter_errors': []}
        if self.previous:
            delta = (now-self.previous[0]) / 1e9
            data['app_cpu_pct'] = 100 * (cpu-self.previous[1]) / delta / (os.cpu_count() or 1)
        self.previous = now, cpu
        try:
            if os.name == 'nt':
                memory, times = _windows_memory()
                data.update(memory)
                if self.previous_system:
                    idle, kernel, user = [a-b for a, b in zip(times, self.previous_system)]
                    total = kernel+user
                    data['system_cpu_pct'] = 100 * (total-idle) / total if total else None
                self.previous_system = times
            else:
                import psutil
                process = psutil.Process()
                data.update(app_ram_mib=process.memory_info().rss / 2**20,
                            system_ram_mib=psutil.virtual_memory().used / 2**20,
                            system_available_mib=psutil.virtual_memory().available / 2**20,
                            system_cpu_pct=psutil.cpu_percent())
        except (OSError, ImportError):
            data['counter_errors'].append('host_counter_unavailable')
        try:
            output = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,temperature.gpu',
                                     '--format=csv,noheader,nounits', '-i', '0'],
                                    capture_output=True, text=True, timeout=2,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if output.returncode:
                raise ValueError('GPU counter unavailable')
            for key, value in zip(['gpu_pct', 'vram_mib', 'gpu_temp_c'], output.stdout.strip().split(',')):
                try:
                    data[key] = float(value.strip())
                except ValueError:
                    data['counter_errors'].append(key + '_unavailable')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            data['counter_errors'].append('gpu_counter_unavailable')
        self.samples.append(data)
        self.emit(data)
        return data

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                start = time.monotonic()
                self.sample()
                self.stop_event.wait(max(0, self.interval-(time.monotonic()-start)))
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)

    def summary(self, start_ns=0, end_ns=2**63-1):
        rows = [s for s in self.samples if start_ns <= s['t_ns'] <= end_ns]
        result = {'resource_sample_count': len(rows), 'gpu_scope': 'whole_device',
                  'app_cpu_scope': 'host_process_normalized_all_cores'}
        for key in ['app_cpu_pct', 'system_cpu_pct', 'app_ram_mib', 'system_ram_mib', 'gpu_pct', 'vram_mib']:
            values = [s[key] for s in rows if s[key] is not None]
            result[key+'_avg'] = statistics.mean(values) if values else None
            result[key+'_max'] = max(values) if values else None
        return result
