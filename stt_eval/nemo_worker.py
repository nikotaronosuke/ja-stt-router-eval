"""Local stdio-only GPU worker. Never receives references or cloud credentials."""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import sys
import threading
import time

protocol_stdout = sys.stdout
write_lock = threading.Lock()


def emit(value):
    with write_lock:
        protocol_stdout.write(json.dumps(value, ensure_ascii=False)+'\n')
        protocol_stdout.flush()


def main():
    emit({'kind':'started','pid':os.getpid()})
    parser = argparse.ArgumentParser()
    parser.add_argument('--precision', choices=['fp32', 'fp16', 'bf16'], default='fp32')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    temporary = root / '.cache' / 'nemo-runtime'
    temporary.mkdir(parents=True, exist_ok=True)
    for variable in ['OPENAI_API_KEY', 'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN']:
        os.environ.pop(variable, None)
    os.environ.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                      HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                      WANDB_MODE='disabled', OTEL_SDK_DISABLED='true',
                      TMPDIR=str(temporary),
                      HF_HOME=str(root / '.cache' / 'huggingface'),
                      NEMO_CACHE_DIR=str(root / '.cache' / 'nemo'),
                      MPLCONFIGDIR=str(root / '.cache' / 'matplotlib'))
    # Third-party logging cannot corrupt the JSONL protocol.
    sys.stdout = sys.stderr
    import numpy as np
    import psutil
    import torch
    import nemo.collections.asr as nemo_asr
    if not torch.cuda.is_available():
        raise RuntimeError('cuda_unavailable')
    checkpoints = list((root / 'models' / 'parakeet-ja').glob('*.nemo'))
    if len(checkpoints) != 1:
        raise RuntimeError('model_checkpoint_missing')
    process = psutil.Process()
    process.cpu_percent()
    stop = threading.Event()

    def sample():
        while not stop.wait(1):
            swap = psutil.swap_memory()
            vmstat = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
            emit({'kind': 'resource', 'scope': 'nemo_worker',
                  'worker_cpu_pct': process.cpu_percent(),
                  'worker_ram_mib': process.memory_info().rss / 2**20,
                  'cuda_allocated_mib': torch.cuda.memory_allocated() / 2**20,
                  'cuda_reserved_mib': torch.cuda.memory_reserved() / 2**20,
                  'wsl_available_mib': psutil.virtual_memory().available / 2**20,
                  'wsl_swap_used_mib': swap.used / 2**20,
                  'wsl_swap_in_bytes': swap.sin, 'wsl_swap_out_bytes': swap.sout,
                  'wsl_oom_kill_count': int(vmstat.get('oom_kill', 0))})
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started = time.perf_counter()
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    from nemo.core.connectors.save_restore_connector import SaveRestoreConnector
    connector = SaveRestoreConnector()
    extracted = root / '.cache' / 'parakeet-extracted'
    if (extracted / 'extraction.json').is_file():
        provenance = json.loads((root / 'models' / 'parakeet-ja' / 'provenance.json').read_text(encoding='utf-8'))
        prepared = json.loads((extracted / 'extraction.json').read_text(encoding='utf-8'))
        if prepared['checkpoint_sha256'] != provenance['sha256']:
            raise RuntimeError('model_checkpoint_mismatch')
        connector.model_extracted_dir = str(extracted)
    model = nemo_asr.models.ASRModel.restore_from(str(checkpoints[0]), map_location='cuda',
                                                 save_restore_connector=connector)
    model.eval()
    dtype = {'fp32': torch.float32, 'fp16': torch.float16, 'bf16': torch.bfloat16}[args.precision]

    def infer(pcm, draft_timestamps=False):
        values = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.
        # The source turn may be 30 seconds, plus shared 1-second trailing silence.
        if not len(values) or len(values) > 31*16000:
            raise ValueError('invalid_audio_size')
        autocast = torch.autocast('cuda', dtype=dtype) if args.precision != 'fp32' else contextlib.nullcontext()
        if draft_timestamps:
            import copy
            original_decoding = copy.deepcopy(model.cfg.decoding)
        try:
            with torch.inference_mode(), autocast:
                results = model.transcribe([values], batch_size=1, verbose=False,
                    **({'timestamps':True, 'return_hypotheses':True} if draft_timestamps else {}))
        finally:
            if draft_timestamps:
                model.change_decoding_strategy(original_decoding, decoder_type=model.cur_decoder, verbose=False)
        if isinstance(results, tuple):
            results = results[0]
        hypothesis = results[0]
        text = hypothesis.text if hasattr(hypothesis, 'text') else str(hypothesis)
        if draft_timestamps:
            stamps = getattr(hypothesis, 'timestamp', None)
            if not isinstance(stamps, dict):
                stamps = getattr(hypothesis, 'timestep', {})
            chars = []
            for item in stamps.get('char', []):
                token = item.get('char', '')
                if isinstance(token, list):
                    token = ''.join(token)
                if 'start' in item and 'end' in item:
                    chars.append({'text':str(token), 'start_s':float(item['start']), 'end_s':float(item['end'])})
            return {'text':text, 'chars':chars, 'review':'model_estimate_only'}
        return text

    infer(bytes(32000))
    torch.cuda.synchronize()
    emit({'kind': 'ready', 'metadata': {'model_load_warmup_s': time.perf_counter()-started,
          'torch_version': torch.__version__, 'cuda_version': torch.version.cuda,
          'gpu_name': torch.cuda.get_device_name(), 'tf32': False,
          'pre_extracted_checkpoint':connector.model_extracted_dir is not None}})
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get('quit'):
                break
            request = message.get('request')
            try:
                if message.get('operation') == 'release_unused_cache':
                    import gc
                    before = {'worker_ram_mib':process.memory_info().rss/2**20,
                              'cuda_allocated_mib':torch.cuda.memory_allocated()/2**20,
                              'cuda_reserved_mib':torch.cuda.memory_reserved()/2**20}
                    gc.collect()
                    torch.cuda.synchronize()
                    torch.cuda.empty_cache()
                    after = {'worker_ram_mib':process.memory_info().rss/2**20,
                             'cuda_allocated_mib':torch.cuda.memory_allocated()/2**20,
                             'cuda_reserved_mib':torch.cuda.memory_reserved()/2**20}
                    emit({'kind':'maintenance','operation':'release_unused_cache',
                          'before':before,'after':after})
                    emit({'kind':'result','request':request,'text':''})
                    continue
                pcm = base64.b64decode(message['audio'], validate=True)
                if message.get('operation') == 'draft_timestamps':
                    emit({'kind':'draft_result','request':request,'draft':infer(pcm,True)})
                    continue
                emit({'kind': 'result', 'request': request, 'text': infer(pcm)})
            except Exception as error:
                import traceback
                frame = traceback.extract_tb(error.__traceback__)[-1]
                emit({'kind': 'error', 'request': request, 'code': 'inference_failed',
                      'cause': type(error).__name__.lower(),
                      'location': f'{Path(frame.filename).name}:{frame.lineno}'})
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Known internal codes only; never dump exception messages or paths.
        known = str(error) if str(error) in ['cuda_unavailable', 'model_checkpoint_missing'] else 'worker_initialization_failed'
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        emit({'kind': 'fatal', 'code': known, 'cause':type(error).__name__.lower(),
              'location':f'{Path(frames[-1].filename).name}:{frames[-1].lineno}' if frames else None})
        raise SystemExit(1)
