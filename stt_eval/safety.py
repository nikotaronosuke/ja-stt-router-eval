"""Keep credentials out of artifacts; cumulative audio budget is checked before send."""
from __future__ import annotations

import json
import math
import os
import re
import threading
from pathlib import Path

PRICE_USD_PER_MINUTE = .017
PRICE_VERIFIED_ON = '2026-09-16'


class DiagnosticError(RuntimeError):
    pass


def local_worker_environment(source=None):
    """An allowlist excludes all service credentials before child process creation."""
    source = os.environ if source is None else source
    allowed = {'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP','COMSPEC',
               'USERPROFILE','HOMEDRIVE','HOMEPATH','LOCALAPPDATA','APPDATA',
               'PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA','SYSTEMDRIVE',
               'NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE'}
    # Inspect names first; never read the values of disallowed credentials.
    env = {k:source[k] for k in source if k.upper() in allowed}
    env.update(PYTHONUTF8='1', HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
               TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    return env


def safe_error(error: BaseException) -> str:
    # Provider exception strings can contain headers, URLs, identifiers or transcript.
    if isinstance(error, DiagnosticError):
        return safe_code(str(error))
    response = getattr(error, 'response', None)
    status = getattr(response, 'status_code', None)
    if isinstance(status, int):
        return 'http_' + str(status)
    return type(error).__name__


def safe_code(code) -> str:
    return code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code) else 'provider_error'


class AudioBudget:
    def __init__(self, path: Path, limit_usd: float):
        if not math.isfinite(limit_usd) or limit_usd <= 0:
            raise ValueError('positive finite budget required')
        self.path, self.limit, self.lock = path, limit_usd, threading.Lock()
        self.minutes = 0.
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('price_usd_per_minute') != PRICE_USD_PER_MINUTE:
                raise ValueError('budget rate changed; manual reconciliation required')
            self.minutes = float(data['reserved_audio_minutes'])
            if not math.isfinite(self.minutes) or self.minutes < 0:
                raise ValueError('invalid budget ledger')

    def reserve(self, seconds: float):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('invalid audio duration')
        with self.lock:
            proposed = self.minutes + seconds / 60
            if proposed * PRICE_USD_PER_MINUTE > self.limit:
                raise RuntimeError('audio_budget_exceeded')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {'reserved_audio_minutes': proposed, 'price_usd_per_minute': PRICE_USD_PER_MINUTE,
                    'price_verified_on': PRICE_VERIFIED_ON,
                    'estimated_usd': proposed*PRICE_USD_PER_MINUTE,
                    'note': 'estimate; conservatively reserved before send; not provider invoice'}
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
            os.replace(temporary, self.path)
            self.minutes = proposed
