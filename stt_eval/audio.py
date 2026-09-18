from __future__ import annotations

import hashlib
import math
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioChunk:
    sequence: int
    pcm16: bytes
    t_ns: int
    stream: str = 'fixture'
    rate: int = 16000


class StreamingResampler:
    """Causal FIR. State persists across chunks; no per-chunk padding/lookahead."""

    def __init__(self, source_rate: int, target_rate: int):
        import numpy as np
        from scipy.signal import firwin
        gcd = math.gcd(source_rate, target_rate)
        self.up, self.down = target_rate // gcd, source_rate // gcd
        self.phase = 0
        self.identity = self.up == self.down
        taps = 20 * max(self.up, self.down) + 1
        self.kernel = (np.array([1.]) if self.identity else
                       firwin(taps, 1 / max(self.up, self.down), window=('kaiser', 5.0)) * self.up)
        self.state = np.zeros(len(self.kernel) - 1)
        self.delay_ms = (len(self.kernel)-1) / (2 * source_rate * self.up) * 1000

    def process(self, values):
        import numpy as np
        from scipy.signal import lfilter
        values = np.asarray(values, dtype=np.float64)
        if self.identity or not len(values):
            return values.copy()
        upsampled = np.zeros(len(values) * self.up)
        upsampled[::self.up] = values
        filtered, self.state = lfilter(self.kernel, [1.], upsampled, zi=self.state)
        output = filtered[self.phase::self.down]
        self.phase = (self.phase - len(filtered)) % self.down
        return output

    def pcm16(self, pcm: bytes) -> bytes:
        import numpy as np
        values = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768.
        return encode_pcm16(self.process(values))


def encode_pcm16(values) -> bytes:
    import numpy as np
    return np.rint(np.clip(values, -1., 32767 / 32768) * 32768).astype('<i2').tobytes()


def load_fixture(path: Path) -> tuple[bytes, dict]:
    import numpy as np
    from scipy.signal import resample_poly
    with wave.open(str(path), 'rb') as reader:
        rate, channels, width = reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
        if width != 2 or reader.getcomptype() != 'NONE':
            raise ValueError('fixture must be uncompressed PCM16 WAV')
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
            raise ValueError('unsupported fixture format')
        frames = reader.getnframes()
        if frames > rate * 30:
            raise ValueError('a benchmark turn must be <= 30 seconds')
        raw = reader.readframes(frames)
        if len(raw) != frames * channels * width:
            raise ValueError('truncated WAV')
    values = np.frombuffer(raw, dtype='<i2').astype(np.float64).reshape(-1, channels).mean(axis=1) / 32768
    if rate != 16000:
        gcd = math.gcd(rate, 16000)
        values = resample_poly(values, 16000 // gcd, rate // gcd)
    canonical = encode_pcm16(values)
    return canonical, {'source_rate': rate, 'source_channels': channels,
                       'canonical_rate': 16000, 'canonical_sha256': hashlib.sha256(canonical).hexdigest(),
                       'duration_s': len(canonical) / 32000, 'source_sha256': hashlib.sha256(raw).hexdigest()}


def validate_manifest(path: Path, require_audio: bool = True) -> list[dict]:
    import json
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version') != 1 or not isinstance(data.get('tests'), list) or not data['tests']:
        raise ValueError('invalid manifest')
    ids = set()
    for test in data['tests']:
        if not isinstance(test.get('test_id'), str) or test['test_id'] in ids:
            raise ValueError('test IDs must be unique strings')
        ids.add(test['test_id'])
        for key in ['condition', 'reference_text', 'audio_kind']:
            if not isinstance(test.get(key), str) or not test[key]:
                raise ValueError('missing test metadata: ' + key)
        if test['audio_kind'] not in ['human', 'synthetic_smoke']:
            raise ValueError('audio_kind must distinguish human from synthetic_smoke')
        for key in ['keywords', 'proper_nouns', 'decision_keywords']:
            if not isinstance(test.get(key), list):
                raise ValueError('missing term list: ' + key)
            for term in test[key]:
                aliases = term if isinstance(term, list) else [term]
                if not aliases or any(not isinstance(a, str) or not a.strip() for a in aliases):
                    raise ValueError('empty/invalid term')
        audio = (path.parent / test['audio']).resolve()
        if require_audio and not audio.is_file():
            raise FileNotFoundError('fixture audio missing: ' + test['test_id'])
        test['_audio_path'] = audio
    return data['tests']
