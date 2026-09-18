"""Obtain a fixed public human-reading subset from Google FLEURS; never execute dataset code.

    python scripts/prepare_fleurs.py

Downloads the ja_jp test split metadata and archive from the Hugging Face dataset
repository, keeps the first 40 unique utterances of 3 to 15 seconds in archive
order, converts them to PCM16 mono 16 kHz, and records provenance (revision,
license, hashes) in `fixtures/manifests/fleurs-ja-40.json`. The selection happens
before any recognizer output exists.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import urllib.request
import wave
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.audio import encode_pcm16  # noqa: E402

MANIFEST = ROOT / 'fixtures' / 'manifests' / 'fleurs-ja-40.json'
DESTINATION = ROOT / 'fixtures' / 'audio' / 'fleurs'
SOURCE = 'https://huggingface.co/datasets/google/fleurs'
API = 'https://huggingface.co/api/datasets/google/fleurs'
SUBSET = 40


def main():
    if MANIFEST.exists():
        raise SystemExit('Existing manifest preserved')
    with urllib.request.urlopen(API, timeout=60) as response:
        revision = json.load(response)['sha']
    base = f'{SOURCE}/resolve/{revision}/data/ja_jp/'
    with urllib.request.urlopen(base + 'test.tsv', timeout=60) as response:
        metadata = response.read()
    entries = {}
    for line in metadata.decode('utf-8').splitlines():
        index, filename, reference, _normalized, _, samples, _gender = line.split('\t')
        if 3 <= int(samples) / 16000 <= 15:
            entries[filename] = (index, reference)
    DESTINATION.mkdir(parents=True, exist_ok=True)
    tests = []
    seen = set()
    with urllib.request.urlopen(base + 'audio/test.tar.gz', timeout=120) as response:
        with tarfile.open(fileobj=response, mode='r|gz') as archive:
            for member in archive:
                name = Path(member.name).name
                if not member.isfile() or name not in entries or member.size > 5_000_000:
                    continue
                index, reference = entries[name]
                if index in seen:
                    continue
                raw = archive.extractfile(member).read()
                rate, values = wavfile.read(io.BytesIO(raw))
                if rate != 16000 or values.ndim != 1:
                    continue
                if np.issubdtype(values.dtype, np.integer):
                    limit = max(abs(np.iinfo(values.dtype).min), np.iinfo(values.dtype).max)
                    values = values.astype(np.float64) / limit
                pcm = encode_pcm16(values)
                label = f'fleurs{len(tests) + 1:02}'
                with (DESTINATION / (label + '.wav')).open('xb') as stream:
                    with wave.open(stream, 'wb') as writer:
                        writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                        writer.writeframes(pcm)
                seen.add(index)
                tests.append({'test_id': label, 'audio': '../audio/fleurs/' + label + '.wav',
                              'condition': 'public_read_speech', 'audio_kind': 'human', 'reference_text': reference,
                              'reference_review': 'dataset_reference', 'keywords': [], 'proper_nouns': [],
                              'decision_keywords': [], 'source_filename': name,
                              'source_sha256': hashlib.sha256(raw).hexdigest()})
                if len(tests) == SUBSET:
                    break
    if len(tests) != SUBSET:
        raise SystemExit('Incomplete subset; inspect before rerunning')
    provenance = {'dataset': 'google/fleurs', 'language': 'ja_jp', 'split': 'test', 'revision': revision,
                  'source': SOURCE, 'license': 'CC-BY-4.0', 'attribution': 'FLEURS, Conneau et al., Google, 2022',
                  'selection': 'First 40 unique utterances in archive order, 3..15 seconds; before any STT output',
                  'changes': 'Convert source WAV to PCM16 mono 16 kHz; retain dataset raw reference',
                  'metadata_sha256': hashlib.sha256(metadata).hexdigest(),
                  'limitations': 'Public read speech, not natural mumbling/fast speech. '
                                 'Reference not independently audited.'}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({'schema_version': 1, 'provenance': provenance, 'tests': tests},
                                   ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'prepared': len(tests), 'audio_kind': 'human', 'license': 'CC-BY-4.0'}))


if __name__ == '__main__':
    main()
