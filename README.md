# Japanese STT & LLM Router Evaluation

Windows 上で、日本語音声認識（STT）と「短い発話 → 限定された候補ID」のルーティングを、
再現可能な条件で評価するための実験用リポジトリです。

## What this evaluates

### Speech recognition

- CER（NFKC・casefold・空白と句読点の除去後の文字誤り率）
- keyword recovery（事前に固定した重要語の回収率。表記の別名は明示したものだけを認める）
- first-partial latency（最初の非 final が届くまで）と keyword-end latency（人が確認した語の言い終わりから最初の表示まで）
- end-to-end latency、partial の修正回数、final の安定時刻
- resource usage（ホスト／WSL worker／GPU 全体の CPU・RAM・VRAM、初期化時間、Windows のコミット余裕）
- 経路：WASAPI loopback（無音 render 維持の診断を含む）、直接マイク、会議アプリの相手側音声

### Routing

- expected hit / acceptable hit
- wrong display（主要指標。accuracy に混ぜない）
- correct abstain / missed but safe
- category breakdown（10カテゴリ、242問、16候補）
- latency / tokens / cost（hosted provider）

## Design principles

- abstain is a valid result
- wrong selection is measured separately
- provider scores are not treated as calibrated probabilities
- evaluation fixtures are sealed before provider comparison
- missing values are counted, never substituted with zero; first trials give quality, repeats give stability
- nothing personal enters a fixture, a log, or a request: fixtures are fictional, audio and results stay local

## Repository layout

| Path | Content |
| --- | --- |
| `router_eval/` | router contract, keyword baseline, evaluation-only OpenAI router, sealed-dataset loader, runner, pre-send check |
| `stt_eval/` | audio pipeline, WASAPI capture, engine adapters (WSL NeMo worker, hosted transcription, sherpa-onnx candidate), comparison harness, metrics, resource sampling, localhost recording/review pages |
| `fixtures/router-eval-v1/` | sealed routing evaluation set (fictional) |
| `fixtures/test-plan.json`, `fixtures/natural-speech-prompts.json` | neutral sentences and recording guides for the STT fixtures |
| `scripts/` | runners, fixture preparation, aggregation, environment setup, fixture validation, privacy scan |
| `tests/` | unit tests (platform-independent; Windows-only cases skip themselves) |
| `docs/` | methodology, historical STT measurements, router evaluation, environment |

## Quick start

```
python -m pip install .
python -m unittest discover -s tests -t . -v
python scripts/validate_router_fixture.py
python scripts/run_router_eval.py --by-category --no-write
python scripts/run_router_eval.py --provider openai --dry-run
```

The keyword baseline and the tests need no audio device, no model and no network.
The STT harness needs the Windows and WSL environments described in
[docs/environment.md](docs/environment.md); the hosted engines need an API key in
`OPENAI_API_KEY`, an explicit budget and an explicit `--confirm-send`.

## Documentation

- [docs/methodology.md](docs/methodology.md) — definitions, fairness rules, what a number may and may not mean
- [docs/stt-evaluation.md](docs/stt-evaluation.md) — historical measurements of the STT harness, with their conditions
- [docs/router-evaluation.md](docs/router-evaluation.md) — the routing evaluation set, the runner, and the keyword baseline
- [docs/environment.md](docs/environment.md) — hardware, software versions, setup, licenses of dependencies and data
- [fixtures/router-eval-v1/README.md](fixtures/router-eval-v1/README.md) — labelling rules of the sealed set

## What is not in this repository

Audio, audio manifests, recordings, transcripts, raw logs, model checkpoints,
virtual environments, API keys and per-provider measurement results. The historical
STT figures in `docs/` therefore cannot be regenerated from this repository alone,
and no hosted-router result is published here.

## Status

Research / evaluation harness. Not a production application.

## License

MIT License. See [LICENSE](LICENSE).
