# Owner Decision Log

[日本語](OWNER_DECISIONS.md) | English

Five decisions are worth keeping because they show how the evaluation was protected, not merely what the harness can do.

## 1. Split the evaluation harness out before product integration

Putting STT or routing directly into the product would mix UI, audio path, prompt, network, and hardware effects.

The evaluation repository was created first so model choices could be compared with common inputs and common metrics.

**Evidence:** [initial harness](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/d0b81f0fe35ba9dc0591f37e1a31eab96132a5a6)

## 2. Treated wrong display as worse than a safe miss

One accuracy number would make "show nothing" and "show the wrong candidate" look like the same error.

In the target UI, the latter is more harmful.

The runner therefore keeps five outcomes and reports `wrong_display_rate` separately.

**Evidence:** [fixture README](../fixtures/router-eval-v1/README.md#判定の5分類)

## 3. Sealed the fixture before provider comparison

If labels or utterances are changed after seeing model results, the benchmark can drift toward the model.

SHA-256 values for `candidates.json` and `questions.json` are fixed in `seal.json`, and the loader rejects modified sealed fixtures.

A changed fixture becomes a new dataset.

**Evidence:** [fixture seal](../fixtures/router-eval-v1/README.md#封印seal)

## 4. Publicly corrected the claim that the fixture was hand-labelled

After publication, the documentation was found to imply manual gold labelling.

In reality, Claude generated the utterances, candidates, labels, and notes, and a human did not individually approve all 242 items.

The sealed fixture was left intact; the provenance documentation was corrected and made authoritative.

**Evidence:** [provenance correction](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/eef45514db141fa48020c7a9e27f35c239484124)

## 5. Kept missing measurements missing, and labelled non-reproducible history honestly

No first partial is not 0 ms latency.

Missing observations stay missing and percentile reports include observed / total counts.

Past STT figures whose original audio, manifests, and raw logs are not public are explicitly labelled **historical measurements**, not fully reproducible results from this repository alone.

**Evidence:** [methodology](methodology.md)
