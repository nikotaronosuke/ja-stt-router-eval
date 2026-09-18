# 環境

`docs/stt-evaluation.md` の測定が行われた環境と、このリポジトリのコードを動かすための構成。
バージョンは 2026-09-16 時点の実測環境の値で、再現時には改めて確認する。

## ハードウェア（測定時）

| 項目 | 値 |
| --- | --- |
| CPU | AMD Ryzen 5 3600（利用者申告） |
| RAM | 16 GB |
| GPU | NVIDIA GeForce RTX 3060 12 GB（nvidia-smi で 12288 MiB、ドライバー 591.86） |
| ストレージ | NVMe SSD |
| OS | Windows 11 Home |

16 GB という物理メモリは、ローカルモデルとブラウザー・会議アプリの併用余裕に直接効く。
`stt_eval/gated_benchmark.py` の停止閾値はこの PC 向けの値で、公式要件ではない。

## ソフトウェア構成

### Windows 側（Python 3.13）

音声取得、hosted エンジン、計測、集計、ローカルの録音・確認ページ。

| パッケージ | バージョン | ライセンス（配布メタデータ） | 用途 |
| --- | --- | --- | --- |
| numpy | 2.2.4 | BSD 系（同梱コンポーネントの条件を LICENSE に収録） | PCM 変換・集計 |
| scipy | 1.15.2 | BSD 系（同上） | 因果 FIR、相関、スペクトル |
| websockets | 16.0 | BSD-3-Clause | hosted transcription session |
| PyAudioWPatch | 0.2.12.8 | Apache-2.0 | WASAPI loopback とマイク |
| psutil | 7.2.2 | BSD-3-Clause | プロセス・メモリ計測、run lock |

`requirements-windows.txt` に固定している。`tkinter` は診断 UI（`python -m stt_eval ui`）だけが使う。

```powershell
.\scripts\setup_windows.ps1 -ApprovedPackages
```

### WSL 側（Ubuntu、Python 3.12.3）

NVIDIA NeMo の GPU worker。Windows 側とは JSONL の標準入出力だけでつながり、待受ポートを作らない。

| 項目 | 値 |
| --- | --- |
| NeMo Toolkit | 3.0.0（Apache-2.0） |
| PyTorch / torchaudio | 2.11.0+cu128（BSD-3-Clause。同梱 CUDA 依存は各条項に従う） |
| 解決済み依存一覧 | `requirements-nemo-resolved.txt` |
| モデル | `nvidia/parakeet-tdt_ctc-0.6b-ja`（CC-BY-4.0）。重みは変更しない |
| 精度・デコーダ | FP32 / TDT、TF32 無効、CPU threads 4 |

```powershell
$wslRoot = (wsl -d Ubuntu -- wslpath -a -u (Get-Location).Path).Trim()
wsl -d Ubuntu -- bash "$wslRoot/scripts/setup_wsl.sh" --approved-packages
wsl -d Ubuntu -- "$wslRoot/.venv-wsl/bin/python" "$wslRoot/scripts/download_model.py" --approved-download
.\.venv\Scripts\python.exe scripts/extract_model.py
```

チェックポイントは公式配布元からのみ取得し、revision と SHA-256 を `models/parakeet-ja/provenance.json`
（Git 管理外）に記録する。Windows 側で一度展開して NeMo の `model_extracted_dir` を使うのは、
WSL から Windows 側フォルダーを展開する待ち時間を減らすためで、重みは変更しない。

worker は `stt_eval/nemo_worker.py`。認証系の環境変数は子プロセスへ渡さず（`stt_eval/safety.py` の
許可リスト）、Hugging Face のオフライン設定と telemetry 無効を強制する。

### 軽量候補（Windows、独立した `.venv-sherpa`）

| 項目 | 値 |
| --- | --- |
| sherpa-onnx / sherpa-onnx-core | 1.13.8（Apache-2.0） |
| numpy | 2.5.3 |
| モデル | `sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8`（元モデルは NVIDIA、CC-BY-4.0） |
| 実行 | CPU provider、threads 4、greedy CTC |

```
python scripts/install_sherpa_candidate.py --approved-local-install
```

wheel と model archive は公開された SHA-256 と照合してから展開する（`models/sherpa-candidate/`、Git 管理外）。

## 公開音声データ

| データ | 出所 | ライセンス | 変更 |
| --- | --- | --- | --- |
| FLEURS ja_jp test（40 文） | Google FLEURS（Conneau et al., 2022） | CC-BY-4.0 | PCM16 mono 16 kHz へ変換。参照文はデータセットのまま |

`scripts/prepare_fleurs.py` が出所・revision・hash を manifest に記録する。`scripts/prepare_comparison.py`
が 18 dB 減衰と白色雑音 SNR 10 dB の派生条件を決定的に生成する（seed を manifest に残す）。

## hosted transcription

| 項目 | 値 |
| --- | --- |
| モデル | `gpt-live-transcribe`（Realtime transcription session） |
| 入力 | PCM16 / mono / 24 kHz、`languages: ["ja"]`、`delay` は minimal / low / medium / high / xhigh |
| 単価（公式確認済み、2026-09-16） | USD 0.017 / 音声分 |
| 接続 | 55 分を超えた発話境界でローテーション（一般ガイドのセッション上限 60 分に合わせた設計） |

単価は `stt_eval/safety.py` の `PRICE_USD_PER_MINUTE` に `PRICE_VERIFIED_ON` とともに置く。
実行前に公式の料金ページで再確認し、送信前に累積音声量を予算台帳へ予約する。予約に基づく推定と
請求実額は別のものとして扱う。

## Router の hosted provider

`router_eval/openai_router.py` は OpenAI Responses API を `urllib` で呼ぶ。既定のモデル ID と
100 万トークン当たりの価格はモジュール内の定数で、書いた時点の値である。実行前に公式のモデル一覧と
料金で確認し、`--model` と価格の上書きで合わせる。

## 計測カウンター

| 対象 | 取得方法 |
| --- | --- |
| ホストプロセス CPU / RAM、システム RAM、コミット | Win32（`GlobalMemoryStatusEx`、`GetProcessMemoryInfo`、`GetSystemTimes`） |
| ページング・コミット限界 | PDH の英語カウンター（`PdhAddEnglishCounterW`） |
| GPU 全体の使用率・VRAM・温度 | `nvidia-smi` |
| WSL worker の CPU / RSS、CUDA allocated / reserved、WSL の swap と OOM kill | worker が psutil と torch で 1 秒ごとに報告 |
| 併用アプリのメモリ | psutil でプロセス名別に合計（chrome / zoom / teams / vmmemwsl） |

## WASAPI について

再生するクライアントがなくなると WASAPI loopback のコールバック自体が止まることがある。live 診断は
最後のパケットから実時間で無音時間を判定し、取得系の耐久試験は `--keep-render-active` で無音の出力
ストリームを維持してフレーム数と実経過時間を照合する。音量や既定デバイス設定は変更しない。
参考: Microsoft の loopback recording の説明、NAudio の WasapiCapture の説明。
