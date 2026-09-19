English | [日本語](README.ja.md)

# Japanese STT & LLM Router Evaluation

An experimental repository for evaluating Japanese speech recognition (STT) and
"short utterance → one of a closed set of candidate ids" routing on Windows under
reproducible conditions.

## What this evaluates

### Speech recognition

- CER (character error rate after NFKC, casefold, and removal of whitespace and punctuation)
- keyword recovery (recall of keywords fixed in advance; only explicitly listed spelling aliases are accepted)
- first-partial latency (until the first non-final result arrives) and keyword-end latency (from the human-confirmed end of the spoken keyword to its first display)
- end-to-end latency, number of partial revisions, and the time at which the final text became stable
- resource usage (CPU, RAM and VRAM of the host, the WSL worker and the whole GPU; initialization time; Windows commit headroom)
- paths: WASAPI loopback (including the silent-render diagnostic), a direct microphone, and the far side of a meeting application

### Routing

- expected hit / acceptable hit
- wrong display (the primary figure; never folded into an accuracy number)
- correct abstain / missed but safe
- category breakdown (10 categories, 242 questions, 16 candidates)
- latency / tokens / cost (hosted provider)

## Design principles

> **Why these rules exist:** [Owner Decision Log](docs/OWNER_DECISIONS.md) *(Japanese)* — why abstention, sealed fixtures, missing-value handling, privacy gates, and evaluation boundaries were chosen.

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
