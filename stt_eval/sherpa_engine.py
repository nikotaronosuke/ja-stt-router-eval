"""Experimental offline CTC adapter; retains the baseline prefix/tail contract.

Runs the sherpa-onnx worker from a separate Windows virtual environment on the
CPU. It speaks the same stdio protocol as the WSL worker, so the harness can
compare the two candidates with identical pacing, interval and scoring.
"""
from __future__ import annotations

from pathlib import Path

from .engines import ParakeetEngine

MODEL_DIR = 'models/sherpa-candidate'
VENV_PYTHON = '.venv-sherpa/Scripts/python.exe'


class SherpaEngine(ParakeetEngine):
    name = 'sherpa_ctc'
    model = 'sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8'
    transmits_audio = False

    def __init__(self, root: Path, *, interval_s=.25, emit=None, command=None):
        root = root.resolve()
        command = command or [str(root / VENV_PYTHON), '-u', str(root / 'stt_eval' / 'sherpa_worker.py'),
                              '--model-dir', str(root / MODEL_DIR)]
        super().__init__(root, interval_s=interval_s, emit=emit, command=command)
        self.metadata.update(decoder='ctc', precision='int8', provider='cpu', cloud_enabled=False)

    async def release_unused_cache(self):
        raise RuntimeError('cache_release_disabled')

    async def draft_timestamps(self, pcm):
        raise RuntimeError('annotation_generation_not_supported')
