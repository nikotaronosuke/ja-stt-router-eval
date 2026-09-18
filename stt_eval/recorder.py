"""Explicit test-only microphone recording, separate from the ephemeral live path.

`TestMicRecorder` records from the default WASAPI input for at most `maximum_s`
seconds and keeps the result in memory as canonical 16 kHz PCM16. `save_fixture`
writes one take as a WAV fixture and appends it to a manifest whose reference text
is marked as requiring review: reading a prompt aloud is not evidence of what was
actually said.
"""
from __future__ import annotations

import json
import math
import re
import threading
import wave
from pathlib import Path

from .audio import encode_pcm16
from .safety import safe_error

CONDITIONS = ('normal', 'quiet', 'fast', 'hesitation', 'noise', 'proper_noun')


class TestMicRecorder:
    def __init__(self, maximum_s=30):
        self.stop_event = threading.Event()
        self.maximum_s = maximum_s
        self.pcm = None
        self.error = None
        self.recording = threading.Event()
        self.thread = None

    def start(self):
        def work():
            import numpy as np
            import pyaudiowpatch as pa
            from scipy.signal import resample_poly
            manager = None
            stream = None
            try:
                manager = pa.PyAudio()
                host = manager.get_host_api_info_by_type(pa.paWASAPI)
                device = manager.get_device_info_by_index(host['defaultInputDevice'])
                rate = int(device['defaultSampleRate'])
                channels = int(device['maxInputChannels'])
                frames = max(1, round(rate * .02))
                stream = manager.open(format=pa.paFloat32, input=True, input_device_index=device['index'],
                                      channels=channels, rate=rate, frames_per_buffer=frames)
                blocks = []
                count = 0
                self.recording.set()
                while not self.stop_event.is_set() and count < rate * self.maximum_s:
                    block = stream.read(frames, exception_on_overflow=True)
                    blocks.append(block)
                    count += frames
                values = np.frombuffer(b''.join(blocks), dtype='<f4').reshape(-1, channels).mean(axis=1)
                if len(values) < rate * .1:
                    raise ValueError('recording too short')
                gcd = math.gcd(rate, 16000)
                if rate != 16000:
                    values = resample_poly(values, 16000 // gcd, rate // gcd)
                self.pcm = encode_pcm16(values)
            except Exception as error:
                self.error = safe_error(error)
            finally:
                self.recording.clear()
                if stream:
                    stream.stop_stream()
                    stream.close()
                if manager:
                    manager.terminate()

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()


def save_fixture(root: Path, test: dict, condition: str, pcm: bytes):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', test.get('test_id', '')):
        raise ValueError('invalid test ID')
    if condition not in CONDITIONS:
        raise ValueError('unknown recording condition')
    if not pcm or len(pcm) % 2 or len(pcm) > 30 * 32000:
        raise ValueError('invalid PCM')
    audio = root / 'fixtures' / 'audio' / 'human'
    manifest = root / 'fixtures' / 'manifests' / 'human-recorded.json'
    audio.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding='utf-8'))
    else:
        data = {'schema_version': 1, 'tests': []}
    for take in range(1, 10000):
        name = f"{test['test_id']}_{condition}_{take:03}"
        destination = audio / (name + '.wav')
        if not destination.exists():
            break
    else:
        raise ValueError('too many takes')
    with destination.open('xb') as stream:
        with wave.open(stream, 'wb') as writer:
            writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            writer.writeframes(pcm)
    data['tests'].append({**test, 'test_id': name, 'condition': condition, 'audio_kind': 'human',
                          'audio': '../audio/human/' + name + '.wav',
                          'reference_review': 'read_aloud_prompt_requires_review'})
    temporary = manifest.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(manifest)
    return manifest
