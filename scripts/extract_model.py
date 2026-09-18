"""Extract the verified official checkpoint once using Windows file I/O.

    python scripts/extract_model.py

NeMo restores a `.nemo` archive by extracting it; doing that from WSL against a
Windows-backed folder is slow. This extracts the checkpoint once on the Windows
side, after checking its SHA-256 against the recorded provenance, so the worker can
use NeMo's `model_extracted_dir`. The weights are not modified.
"""
from __future__ import annotations

import hashlib
import json
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / 'models' / 'parakeet-ja'
DESTINATION = ROOT / '.cache' / 'parakeet-extracted'


def main():
    provenance = json.loads((MODEL_DIR / 'provenance.json').read_text(encoding='utf-8'))
    checkpoint = MODEL_DIR / provenance['file']
    marker = DESTINATION / 'extraction.json'
    if DESTINATION.exists():
        raise SystemExit('Existing extraction preserved')
    started = time.perf_counter()
    with checkpoint.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != provenance['sha256']:
        raise SystemExit('Checkpoint hash mismatch')
    DESTINATION.mkdir()
    count = 0
    with tarfile.open(checkpoint, 'r:*') as archive:
        for member in archive:
            if not member.isfile() and not member.isdir():
                raise ValueError('Only ordinary checkpoint files/directories allowed')
            if not (DESTINATION / member.name).resolve().is_relative_to(DESTINATION.resolve()):
                raise ValueError('Archive path escapes destination')
            archive.extract(member, path=DESTINATION, filter='data')
            count += 1
    elapsed = time.perf_counter() - started
    marker.write_text(json.dumps({'checkpoint_sha256': digest, 'file_count': count, 'extraction_s': elapsed}, indent=2),
                      encoding='utf-8')
    print(json.dumps({'file_count': count, 'extraction_s': elapsed}))


if __name__ == '__main__':
    main()
