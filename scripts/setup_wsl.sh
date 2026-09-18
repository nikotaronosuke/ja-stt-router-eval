#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != "--approved-packages" ]; then
  echo 'Explicit package installation approval required.' >&2
  exit 2
fi
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
export PIP_CACHE_DIR="$project_root/.cache/pip-wsl"
export XDG_CACHE_HOME="$project_root/.cache/wsl"
export TMPDIR="$project_root/.cache/tmp-wsl"
mkdir -p .tools "$TMPDIR"
if [ ! -f .tools/pip.pyz ]; then
  python3 -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/pip/pip.pyz', '.tools/pip.pyz')"
fi
if [ ! -f .venv-wsl/bin/python ]; then
  python3 -m venv --without-pip --copies .venv-wsl
fi
.venv-wsl/bin/python .tools/pip.pyz install 'torch==2.11.0' 'torchaudio==2.11.0' --index-url https://download.pytorch.org/whl/cu128
.venv-wsl/bin/python .tools/pip.pyz install -r requirements-nemo.txt
.venv-wsl/bin/python -c 'import torch,nemo.collections.asr; print({"nemo_import":True,"cuda":torch.cuda.is_available(),"torch":torch.__version__})'
