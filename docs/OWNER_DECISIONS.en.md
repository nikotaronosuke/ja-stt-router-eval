# Owner Decision Log

[日本語](OWNER_DECISIONS.md) | English

Japanese STT & LLM Router Evaluation is not a repository for declaring one model "the winner."

It was separated from the application layer so Japanese speech recognition and
"short utterance → closed candidate id" routing could be compared under explicit, repeatable rules.

This document records the project-owner decisions behind **what counts as a fair comparison,
which failure modes matter most, and which numbers should not be overclaimed**.

---

## 1. Build the evaluation harness before committing to a production model

### Problem

STT or routing can be integrated directly into an application and judged by feel.

But once UI behavior, audio capture, model choice, prompt design, network latency, and hardware load
are all mixed together, it becomes difficult to explain why one path performed better.

### Decision

The evaluation layer was split into its own repository.

It isolates:

- STT
- routing
- fixtures
- metrics
- resource measurement

from the production application.

The priority was to create a common measuring instrument **before** using a model choice as a product assumption.

**Evidence:** [initial evaluation harness](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/d0b81f0fe35ba9dc0591f37e1a31eab96132a5a6)

---

## 2. Feed STT engines from one common source instead of engine-specific inputs

### Problem

If every STT engine receives a separately optimized audio path,
the comparison mixes model quality with preprocessing advantages.

### Decision

The harness creates one normalized reference input and records its hash / conversion conditions.

The methodology uses rules such as:

- one common 16 kHz mono PCM16 source
- explicit causal resampling when another engine requires a different rate
- real-time 20 ms pacing
- one Windows monotonic timing origin
- no subtracting timestamps from unrelated Windows / WSL clocks

The comparison starts from one common reference rather than model-specific convenience.

**Evidence:** [methodology — STT fairness](methodology.md#stt-評価)

---

## 3. Separate first-trial quality from repeat-run stability

### Problem

Repeating the same audio 100 times can make a sample count look much larger.

But 100 plays of one utterance are not 100 independent Japanese utterances.

### Decision

The first trial is used for quality measurement.

Repeated runs measure stability / long-run behavior.

Repeated playback is not counted as additional independent accuracy evidence.

**Evidence:** [methodology — common principles](methodology.md#共通の原則)

---

## 4. Never convert missing measurements to zero

### Problem

If no partial result appears, storing latency as `0` makes aggregation easy.

But:

> no result arrived

and:

> the result arrived immediately

are opposite outcomes.

### Decision

Missing observations remain missing.

Examples include:

- no first partial
- no final
- missing human annotation
- unconfirmed reference

Percentile reporting includes both the number of observed values and the total number of trials.

Clean tables are not allowed to turn missing evidence into good performance.

**Evidence:** [methodology — missing values](methodology.md#共通の原則)

---

## 5. Do not call synthetic or artificially degraded audio "natural speech"

### Problem

Synthetic speech and artificial noise are reproducible and useful.

They do not automatically represent:

- quiet human speech
- fast speech
- hesitation
- meeting codecs
- microphone behavior

### Decision

Artificial conditions remain explicitly labelled as artificial.

Synthetic speech, attenuation, white noise, and degraded fixtures are not presented as proof of natural-speech accuracy.

The broader evaluation plan also separated direct STT, WASAPI capture, and meeting-path tests so
recognizer effects and transport effects would not be conflated.

**Evidence:** [methodology — common principles](methodology.md#共通の原則)

---

## 6. Evaluate routing as candidate selection, not answer generation

### Problem

A router could be given full card text and asked to produce the final user-facing answer.

That would mix two different capabilities:

- selecting the correct memory / card
- generating persuasive prose

### Decision

The router contract is deliberately narrow:

```text
question
previous_question
candidate id / title / triggers
        ↓
selected_id OR abstain
```

Display descriptions, ground-truth labels, and evaluator notes are not sent to the router.

The task is:

> choose safely from a closed candidate set

not:

> write a plausible answer.

**Evidence:** [methodology — Router contract](methodology.md#router-評価)

---

## 7. Treat abstention as a valid correct result

### Problem

A single accuracy number often treats "selected nothing" as failure.

But in a real UI, showing the wrong card can be more harmful than showing nothing.

### Decision

The evaluation uses five mutually exclusive outcomes:

- `expected_hit`
- `acceptable_hit`
- `wrong_display`
- `correct_abstain`
- `missed_but_safe`

The value judgement is explicit:

> **wrong display is worse than a safe miss**

Failing to show a useful candidate and actively showing the wrong candidate are not collapsed into one identical error.

**Evidence:** [fixture README — five outcomes](../fixtures/router-eval-v1/README.md#判定の5分類)

---

## 8. Keep wrong-display rate visible instead of hiding it inside accuracy

### Problem

Two routers can have similar overall accuracy while behaving very differently:

- one abstains too often
- one confidently shows unrelated candidates

Those failure modes have different product consequences.

### Decision

`wrong_display_rate` is a primary metric on its own.

Correct abstention and missed-but-safe are also reported separately.

A high aggregate accuracy should not hide a dangerous display behavior.

**Evidence:** [router evaluation](router-evaluation.md)

---

## 9. Do not treat every provider score as a calibrated probability

### Problem

Different routers may emit different kinds of scores.

For example:

- keyword baseline → number of trigger matches
- hosted model → self-reported relevance / confidence-like score

Converting both to a 0–1 chart would not make them semantically equivalent.

### Decision

Every result records its `score_type`.

Scores are compared only within compatible score types.

The repository deliberately does not manufacture:

- calibration error
- AUC
- probability-quality claims

from scores that are not known to be calibrated probabilities.

**Evidence:** [methodology — score handling](methodology.md#スコアの扱い)

---

## 10. Seal the fixture before provider comparison

### Problem

After seeing model results, it is easy to rationalize changes such as:

> this question was ambiguous, so remove it

or:

> this label should really be different

That can quietly adapt the benchmark to the model being evaluated.

### Decision

Before provider comparison, the dataset is sealed.

`seal.json` records SHA-256 values for:

- `candidates.json`
- `questions.json`

The loader refuses a fixture whose seal no longer matches.

If labels or utterances need to change, the result is treated as a **new dataset** and old provider measurements are not compared against it.

The goal is to prevent post-result benchmark adjustment by mechanism, not by good intentions alone.

**Evidence:** [fixture sealing](../fixtures/router-eval-v1/README.md#封印seal)

---

## 11. Correct the provenance record when the fixture was not actually hand-labelled

### Problem

After publication, wording in the sealed router fixture implied that the gold labels were manually assigned.

That was not accurate.

In reality, Claude via Claude Code generated:

- utterances
- candidates
- triggers
- expected / acceptable labels
- abstain labels
- notes

and a human did **not** individually review and approve all 242 items.

### Decision

The provenance documentation was corrected publicly.

The already-sealed fixture itself was not rewritten, because changing it would create a different dataset.

Instead, the documentation now distinguishes:

- what is mechanically validated
- what was AI-generated
- what was not exhaustively human-reviewed

The goal was not to make the dataset look stronger than its actual provenance.

**Evidence:** [provenance correction](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/eef45514db141fa48020c7a9e27f35c239484124)

---

## 12. Gate hosted-provider runs on privacy checks and budget

### Problem

Even a fictional evaluation fixture can later be modified in ways that accidentally include:

- local paths
- email addresses
- URLs
- labels / notes that should not be sent
- credential-like strings

Hosted evaluation can also spend more API budget than intended.

### Decision

A hosted run requires all of the following:

1. pre-send validation passes
2. estimated cost is within the explicit budget
3. API credentials are available
4. the user explicitly passes `--confirm-send`

The precheck constructs the actual payload and verifies that disallowed content is absent.

A run that reaches its budget mid-execution is not treated as a completed evaluation result.

**Evidence:** [router evaluation — hosted run gates](router-evaluation.md#実行手順)

---

## 13. Label historical STT measurements as historical when the repository cannot reproduce them alone

### Problem

Past STT measurements exist.

But the public repository intentionally does not contain all original materials, such as:

- audio
- audio manifests
- raw transcripts
- raw logs
- model checkpoints
- the complete original runtime environment

Calling those figures fully reproducible from this repository would overstate the evidence.

### Decision

Those results are explicitly described as **historical measurements**.

The README states that they cannot be regenerated from this repository alone.

By contrast, the sealed router fixture and keyword baseline are public and can be rerun.

Reproducible and non-reproducible parts are separated instead of being described with one blanket claim.

**Evidence:** [methodology — historical measurement](methodology.md#共通の原則)

---

## 14. Keep provider-specific results separate from the long-lived evaluation asset

### Problem

A public leaderboard is easy to understand, but hosted-provider results depend on:

- provider terms
- model version
- pricing
- prompt version
- execution date

Those facts can change faster than the evaluation methodology.

### Decision

The long-lived Git-tracked assets are:

- dataset
- methodology
- runner
- validation
- deterministic baseline

Provider-specific measurements are written to Git-ignored artifacts rather than becoming the canonical repository narrative.

The evaluation framework and one moment's model ranking are different assets.

**Evidence:** [router evaluation — hosted results](router-evaluation.md#hosted-provider-の結果について)

---

## What this project prioritizes

Japanese STT & LLM Router Evaluation prioritizes:

- fixing evaluation conditions before product integration
- common input and timing rules for STT
- not inflating accuracy samples with repeated audio
- preserving missing measurements as missing
- not using artificial audio as proof of natural-speech quality
- separating routing from response generation
- treating abstention as a valid safe outcome
- measuring wrong displays separately from safe misses
- not pretending incompatible scores are calibrated probabilities
- sealing fixtures before provider comparison
- correcting provenance mistakes publicly
- gating hosted requests on privacy and cost
- not calling historical results reproducible when the required artifacts are absent
- separating methodology from transient provider rankings

AI-assisted implementation and fixture generation were used during the project.

The important project decision was made **before** the model results:

> define what a fair and safe evaluation means first.
