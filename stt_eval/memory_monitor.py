"""Read-only Windows paging counters and allowlisted process aggregates.

`PagingCounters` reads language-independent PDH counters (pages in/out, paging
file usage, committed bytes and the commit limit). `application_memory` sums the
resident and private memory of a fixed set of coexisting applications by process
name, so a measurement can say how much of the machine's memory was held by a
browser or a meeting client without recording any process identifiers.
"""
from __future__ import annotations

import ctypes

# Coexisting applications whose memory is reported as a group. Process names only;
# nothing about the processes' windows, titles or contents is read.
APPLICATION_GROUPS = {'chrome.exe': 'chrome', 'zoom.exe': 'zoom', 'ms-teams.exe': 'teams',
                      'teams.exe': 'teams', 'vmmemwsl': 'wsl'}


class PagingCounters:
    PATHS = {'pages_input_per_s': r'\Memory\Pages Input/sec',
             'pages_output_per_s': r'\Memory\Pages Output/sec',
             'paging_file_percent': r'\Paging File(_Total)\% Usage',
             'committed_bytes': r'\Memory\Committed Bytes',
             'commit_limit_bytes': r'\Memory\Commit Limit'}

    def __init__(self):
        from ctypes import wintypes
        self.lib = ctypes.WinDLL('pdh')
        self.query = wintypes.HANDLE()
        self.lib.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        self.lib.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t,
                                                   ctypes.POINTER(wintypes.HANDLE)]
        self.lib.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        self.lib.PdhCloseQuery.argtypes = [wintypes.HANDLE]

        class Value(ctypes.Structure):
            _fields_ = [('status', wintypes.DWORD), ('value', ctypes.c_double)]

        self.Value = Value
        self.lib.PdhGetFormattedCounterValue.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Value)]
        if self.lib.PdhOpenQueryW(None, 0, ctypes.byref(self.query)):
            raise OSError('pdh_open_failed')
        self.counters = {}
        for name, path in self.PATHS.items():
            counter = wintypes.HANDLE()
            if self.lib.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(counter)) == 0:
                self.counters[name] = counter
        self.lib.PdhCollectQueryData(self.query)

    def sample(self):
        self.lib.PdhCollectQueryData(self.query)
        result = {}
        for name, counter in self.counters.items():
            value = self.Value()
            code = self.lib.PdhGetFormattedCounterValue(counter, 0x200, None, ctypes.byref(value))
            result[name] = value.value if code == 0 and value.status in [0, 1] else None
        return result

    def close(self):
        self.lib.PdhCloseQuery(self.query)


def application_memory():
    import psutil
    data = {name: {'process_count': 0, 'rss_mib': 0., 'private_mib': 0.} for name in set(APPLICATION_GROUPS.values())}
    for process in psutil.process_iter(['name', 'memory_info']):
        try:
            group = APPLICATION_GROUPS.get((process.info['name'] or '').lower())
            if group:
                memory = process.info['memory_info']
                data[group]['process_count'] += 1
                data[group]['rss_mib'] += memory.rss / 2**20
                data[group]['private_mib'] += getattr(memory, 'private', 0) / 2**20
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return data
