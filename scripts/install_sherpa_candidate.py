"""Explicitly approved, pinned install of the sherpa-onnx CTC/int8 candidate.

    python scripts/install_sherpa_candidate.py --approved-local-install

Creates an isolated `.venv-sherpa`, installs three pinned wheels from PyPI after
checking their published SHA-256, downloads the official model archive from the
sherpa-onnx GitHub release after checking its digest, and extracts only the two
model files. Nothing in the existing environments is changed, and no subprocess
output is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.diagnostics import write_new  # noqa: E402
from stt_eval.safety import local_worker_environment  # noqa: E402

PACKAGES = {'sherpa-onnx': '1.13.8', 'sherpa-onnx-core': '1.13.8', 'numpy': '2.5.3'}
ARCHIVE = 'sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8.tar.bz2'
RELEASE_API = 'https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/asr-models'
RELEASE_DOWNLOAD = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'
MODEL_FILES = ('model.int8.onnx', 'tokens.txt')
VENV_DIR = '.venv-sherpa'
MODEL_DIR = 'models/sherpa-candidate'
CACHE_DIR = '.cache/sherpa-downloads'


def read_json(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def download(url, destination, expected):
    if destination.exists():
        with destination.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest != expected:
            raise ValueError('existing_download_mismatch')
        return
    with urllib.request.urlopen(url, timeout=60) as response, destination.open('xb') as output:
        digest = hashlib.sha256()
        while block := response.read(1024 * 1024):
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != expected:
        raise ValueError('download_mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approved-local-install', action='store_true', required=True)
    parser.parse_args()
    destination = ROOT / VENV_DIR
    model = ROOT / MODEL_DIR
    cache = ROOT / CACHE_DIR
    if destination.exists() or model.exists():
        raise ValueError('candidate_environment_exists')
    cache.mkdir(parents=True, exist_ok=True)
    provenance = {'packages': [], 'sources': ['https://pypi.org', 'https://github.com/k2-fsa/sherpa-onnx'],
                  'runtime_license': 'Apache-2.0', 'model_license': 'CC-BY-4.0',
                  'model_origin': 'https://huggingface.co/nvidia/parakeet-tdt_ctc-0.6b-ja',
                  'provider': 'cpu', 'python_target': '3.13', 'credentials_used': False}
    wheels = []
    for name, version in PACKAGES.items():
        data = read_json(f'https://pypi.org/pypi/{name}/{version}/json')
        candidates = [item for item in data['urls'] if item['filename'].endswith('win_amd64.whl')
                      and ('cp313-cp313-' in item['filename'] or 'py3-none-' in item['filename'])]
        if len(candidates) != 1:
            raise ValueError('wheel_selection_failed')
        artifact = candidates[0]
        url = artifact['url']
        if not url.startswith('https://files.pythonhosted.org/'):
            raise ValueError('unexpected_wheel_source')
        target = cache / artifact['filename']
        download(url, target, artifact['digests']['sha256'])
        wheels.append(target)
        provenance['packages'].append({'name': name, 'version': version, 'filename': target.name,
                                       'sha256': artifact['digests']['sha256'], 'bytes': artifact['size'],
                                       'hash_verified': True})
    release = read_json(RELEASE_API)
    asset = next(item for item in release['assets'] if item['name'] == ARCHIVE)
    url = asset['browser_download_url']
    digest = asset.get('digest', '')
    if not url.startswith(RELEASE_DOWNLOAD) or not digest.startswith('sha256:'):
        raise ValueError('model_source_or_digest_missing')
    download(url, cache / ARCHIVE, digest.removeprefix('sha256:'))
    provenance['archive'] = {'name': ARCHIVE, 'sha256': digest.removeprefix('sha256:'), 'bytes': asset['size'],
                             'hash_verified': True}
    model.mkdir(parents=True, exist_ok=False)
    extracted = {}
    with tarfile.open(cache / ARCHIVE, 'r|bz2') as archive:
        for member in archive:
            name = Path(member.name).name
            if name not in MODEL_FILES:
                continue
            if not member.isfile() or name in extracted:
                raise ValueError('unexpected_model_member')
            with archive.extractfile(member) as source, (model / name).open('xb') as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            with (model / name).open('rb') as source:
                extracted[name] = hashlib.file_digest(source, 'sha256').hexdigest()
    if set(extracted) != set(MODEL_FILES):
        raise ValueError('model_files_missing')
    provenance['model_file_sha256'] = extracted
    env = local_worker_environment()
    subprocess.run([sys.executable, '-m', 'venv', str(destination)], env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    python = destination / 'Scripts' / 'python.exe'
    subprocess.run([str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
                    '--no-index', '--no-deps', '--only-binary=:all:', *(str(path) for path in wheels)],
                   env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = subprocess.run([str(python), '-c',
                            'import json,sys,numpy,sherpa_onnx; print(json.dumps({"python":list(sys.version_info[:3]),'
                            '"numpy":numpy.__version__,"sherpa":sherpa_onnx.__version__}))'],
                           env=env, check=True, capture_output=True, text=True)
    provenance['import_probe'] = json.loads(probe.stdout)
    write_new(model / 'provenance.json', provenance)
    print(json.dumps({'status': 'installed', 'verified_wheels': len(wheels), 'verified_model_archive': True,
                      'model_files': len(extracted), 'import_probe': provenance['import_probe'], 'provider': 'cpu'}))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('{"status":"candidate_install_failed","details":"withheld"}')
        raise SystemExit(1) from None
