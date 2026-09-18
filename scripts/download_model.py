"""Run inside the WSL venv after explicit download approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import urllib.parse

parser = argparse.ArgumentParser()
parser.add_argument('--approved-download', action='store_true')
args = parser.parse_args()
if not args.approved_download:
    parser.error('explicit model download approval required')
root = Path(__file__).resolve().parent.parent
os.environ['HF_HOME'] = str(root / '.cache' / 'huggingface')
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
os.environ['NEMO_CACHE_DIR'] = str(root / '.cache' / 'nemo')
repo = 'nvidia/parakeet-tdt_ctc-0.6b-ja'
with urllib.request.urlopen('https://huggingface.co/api/models/'+repo, timeout=30) as response:
    info = json.load(response)
files = [s['rfilename'] for s in info['siblings'] if s['rfilename'].endswith('.nemo')]
if len(files) != 1:
    raise RuntimeError('expected one official nemo checkpoint')
destination = root / 'models' / 'parakeet-ja'
destination.mkdir(parents=True, exist_ok=True)
if Path(files[0]).name != files[0]:
    raise RuntimeError('unexpected checkpoint filename')
path = destination / files[0]
if path.exists():
    raise RuntimeError('checkpoint already exists; refusing overwrite')
url = 'https://huggingface.co/'+repo+'/resolve/'+info['sha']+'/'+urllib.parse.quote(files[0])
partial = path.with_suffix('.part')
downloaded, last_report = 0, 0
with urllib.request.urlopen(url, timeout=60) as response, partial.open('wb') as target:
    while block := response.read(1024*1024):
        target.write(block)
        downloaded += len(block)
        if downloaded-last_report >= 256*1024*1024:
            print(json.dumps({'downloaded_mib':downloaded//2**20}), flush=True)
            last_report = downloaded
os.replace(partial, path)
digest = hashlib.sha256()
with path.open('rb') as stream:
    for block in iter(lambda: stream.read(1024*1024), b''):
        digest.update(block)
metadata = {'model': repo, 'revision': info['sha'], 'file': path.name,
            'sha256': digest.hexdigest(), 'license': 'CC-BY-4.0',
            'source': 'https://huggingface.co/nvidia/parakeet-tdt_ctc-0.6b-ja', 'modified': False}
(destination / 'provenance.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
print(json.dumps({'model_downloaded': True, 'bytes': path.stat().st_size}))
