"""Private stdio IPC only: launched with stdout PIPE and stderr DEVNULL by the harness.

Never run this worker directly on private audio in an interactive terminal.
No download, reference text, cloud key, or network client is used here.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

MAX_LINE_BYTES = 1400000
MAX_TURN_BYTES = 31 * 32000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-dir', type=Path, required=True)
    args = parser.parse_args()
    output = sys.stdout

    def emit(data):
        output.write(json.dumps(data, ensure_ascii=False) + '\n')
        output.flush()

    emit({'kind': 'started', 'pid': os.getpid()})
    # Native code diagnostics go to stderr, which the parent discards.
    sys.stdout = sys.stderr
    import numpy as np
    import sherpa_onnx
    from resources import _windows_memory
    model = args.model_dir / 'model.int8.onnx'
    tokens = args.model_dir / 'tokens.txt'
    if not model.is_file() or not tokens.is_file():
        emit({'kind': 'fatal', 'code': 'model_missing'})
        return
    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(model), tokens=str(tokens), num_threads=4,
            sample_rate=16000, feature_dim=80, decoding_method='greedy_search',
            debug=False, provider='cpu')
        emit({'kind': 'ready', 'metadata': {'runtime': 'sherpa-onnx', 'runtime_version': sherpa_onnx.__version__,
                                            'decoder': 'ctc', 'precision': 'int8', 'provider': 'cpu',
                                            'cpu_threads': 4, 'streaming_mode': 'buffered_prefix',
                                            'cloud_enabled': False}})
        for line in sys.stdin:
            request = None
            try:
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError()
                message = json.loads(line)
                if message.get('quit'):
                    break
                request = message['request']
                if not isinstance(request, int) or message.get('operation'):
                    raise ValueError()
                pcm = base64.b64decode(message['audio'], validate=True)
                if not pcm or len(pcm) % 2 or len(pcm) > MAX_TURN_BYTES:
                    raise ValueError()
                stream = recognizer.create_stream()
                stream.accept_waveform(16000, np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768)
                recognizer.decode_stream(stream)
                text = stream.result.text
                memory, _ = _windows_memory()
                emit({'kind': 'resource', 'scope': 'sherpa_worker', 'worker_ram_mib': memory['app_ram_mib'],
                      'cuda_allocated_mib': 0., 'cuda_reserved_mib': 0.})
                emit({'kind': 'result', 'request': request, 'text': text})
                del stream, pcm, text, message
            except Exception:
                emit({'kind': 'error', 'request': request, 'code': 'local_inference_failed'})
    except Exception:
        emit({'kind': 'fatal', 'code': 'local_model_failed'})


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        raise SystemExit(1) from None
