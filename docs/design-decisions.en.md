# Evaluation design decisions

English | [日本語](design-decisions.md)

This document was organized retrospectively on 2026-09-20 from existing public commits, the evaluation methodology, and the sealed-fixture documentation. It is not a contemporaneous decision log. Each item is limited to claims that can be checked against the linked public material.

## 1. Treat wrong display as worse than a safe miss

A single accuracy number would make "show nothing" and "show the wrong candidate" look like the same kind of error.

For the target UI, displaying the wrong candidate is treated as the more harmful failure. The runner therefore keeps five outcomes:

- `expected_hit`
- `acceptable_hit`
- `wrong_display`
- `correct_abstain`
- `missed_but_safe`

and reports `wrong_display_rate` separately as a primary metric.

**Evidence:** [fixture README](../fixtures/router-eval-v1/README.md#判定の5分類)

## 2. Seal the fixture before provider comparison

Changing utterances or labels after seeing provider results would let the benchmark drift toward those results.

SHA-256 values for `candidates.json` and `questions.json` are therefore recorded in `seal.json`, and the default loader rejects a fixture that no longer matches its seal. A changed fixture is treated as a new dataset.

**Evidence:** [fixture seal](../fixtures/router-eval-v1/README.md#封印seal)

## 3. Publicly correct the misleading hand-labelled description

After the initial publication, the fixture documentation was found to imply that the gold labels had been assigned manually.

In reality, Claude generated the fictional utterances, candidates, labels, and notes, and a human did not individually review and approve all 242 items.

The sealed fixture itself was kept unchanged so the dataset would not silently change. The provenance documentation was corrected instead.

**Evidence:** [provenance correction](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/eef45514db141fa48020c7a9e27f35c239484124) / [fixture provenance](../fixtures/router-eval-v1/README.md#作成の経緯provenance)

## 4. Keep missing measurements missing, and label non-reproducible history honestly

If a trial has no first partial, recording it as 0 ms would turn "not observed" into "arrived instantly."

Missing observations therefore remain missing, and percentile reports include observed / total counts.

Past STT figures whose original audio, manifests, and raw logs are not included in the public repository are explicitly labelled **historical measurements**. The repository does not claim those exact figures can be regenerated from the public contents alone.

**Evidence:** [methodology](methodology.md) / [historical STT measurements](stt-evaluation.md)
