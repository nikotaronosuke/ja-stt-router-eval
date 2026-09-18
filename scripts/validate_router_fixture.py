"""Check the router evaluation set's invariants and the keyword baseline's shape.

    python scripts/validate_router_fixture.py                # sealed fixture, all checks
    python scripts/validate_router_fixture.py --unsealed     # while authoring, before sealing
    python scripts/validate_router_fixture.py --show-hits    # list trigger hits per question

Structural invariants (the same ones the unit tests assert):

- every question id is unique, has a known category and a non-empty note
- expected and acceptable ids exist, do not overlap, and an abstaining question has
  no expected id while a non-abstaining one has at least one
- follow_up and very_short questions carry a previous question unless they should abstain
- multiple questions expect at least two ids
- none, distractor, scope_trap and ambiguous questions all should abstain
- every candidate is referenced by at least one expected or acceptable label

Keyword-baseline shape, so the categories mean what their names say:

- every direct question contains the triggers of exactly one candidate (its expected one)
- no none question contains any trigger
- at least one distractor contains a trigger (the baseline must be able to be wrong)

Exit status is non-zero on any finding. Nothing here opens a network connection.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from router_eval.contract import RouterInput  # noqa: E402
from router_eval.dataset import CATEGORIES, DEFAULT_DIR, load_dataset  # noqa: E402
from router_eval.evaluate import classify, evaluate  # noqa: E402
from router_eval.keyword import KeywordRouter, normalize  # noqa: E402

CONTEXT_CATEGORIES = ('follow_up', 'very_short')
ABSTAIN_CATEGORIES = ('none', 'distractor', 'scope_trap', 'ambiguous')
EXPECTED_COUNTS = {'direct': 40, 'paraphrase': 40, 'indirect': 30, 'follow_up': 22, 'multiple': 20,
                   'none': 25, 'ambiguous': 18, 'distractor': 20, 'scope_trap': 15, 'very_short': 12}


def trigger_hits(dataset, question):
    """Which candidates' triggers occur in the question text, and which triggers."""
    text = normalize(question.question)
    hits = {}
    for candidate in dataset.candidates:
        matched = [term for term in candidate.triggers if normalize(term) in text]
        if matched:
            hits[candidate.id] = matched
    return hits


def structural_findings(dataset):
    findings = []
    ids = {candidate.id for candidate in dataset.candidates}
    used = set()
    counts = Counter(question.category for question in dataset.questions)
    for category, expected in EXPECTED_COUNTS.items():
        if counts.get(category, 0) != expected:
            findings.append(f'category_count:{category}:{counts.get(category, 0)}!={expected}')
    for question in dataset.questions:
        if question.category not in CATEGORIES:
            findings.append(f'unknown_category:{question.id}')
        if not question.question.strip():
            findings.append(f'empty_question:{question.id}')
        if not question.notes.strip():
            findings.append(f'empty_notes:{question.id}')
        expected = set(question.expected_ids)
        acceptable = set(question.acceptable_ids)
        if not expected <= ids or not acceptable <= ids:
            findings.append(f'unknown_label_id:{question.id}')
        if expected & acceptable:
            findings.append(f'expected_overlaps_acceptable:{question.id}')
        if question.should_abstain and expected:
            findings.append(f'abstain_with_expected:{question.id}')
        if not question.should_abstain and not expected:
            findings.append(f'no_expected_without_abstain:{question.id}')
        if question.category in CONTEXT_CATEGORIES and not (question.previous_question or question.should_abstain):
            findings.append(f'context_missing:{question.id}')
        if question.category == 'multiple' and len(expected) < 2:
            findings.append(f'multiple_needs_two_expected:{question.id}')
        if question.category in ABSTAIN_CATEGORIES and not question.should_abstain:
            findings.append(f'abstain_category_not_abstaining:{question.id}')
        used |= expected | acceptable
    for candidate_id in sorted(ids - used):
        findings.append(f'candidate_never_labelled:{candidate_id}')
    return findings


def baseline_findings(dataset):
    findings = []
    router = KeywordRouter()
    for question in dataset.questions:
        hits = trigger_hits(dataset, question)
        request = RouterInput(question=question.question, candidates=dataset.views,
                              previous_question=question.previous_question)
        outcome = classify(question, router.route(request))
        if question.category == 'direct':
            if set(hits) != set(question.expected_ids):
                findings.append(f'direct_trigger_mismatch:{question.id}:{sorted(hits)}')
            if outcome != 'expected_hit':
                findings.append(f'direct_not_expected_hit:{question.id}:{outcome}')
        if question.category == 'none' and hits:
            findings.append(f'none_contains_trigger:{question.id}:{sorted(hits)}')
    distractors = [question for question in dataset.questions if question.category == 'distractor']
    if not any(trigger_hits(dataset, question) for question in distractors):
        findings.append('distractor_without_any_trigger')
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validate a router evaluation set')
    parser.add_argument('--dataset', default=None, help='dataset directory (default: fixtures/router-eval-v1)')
    parser.add_argument('--unsealed', action='store_true', help='do not require a matching seal')
    parser.add_argument('--show-hits', action='store_true', help='print trigger hits for every question')
    args = parser.parse_args(argv)
    directory = Path(args.dataset) if args.dataset else DEFAULT_DIR
    dataset = load_dataset(directory, require_seal=not args.unsealed)
    findings = structural_findings(dataset) + baseline_findings(dataset)
    if args.show_hits:
        for question in dataset.questions:
            hits = trigger_hits(dataset, question)
            marker = '' if not hits else ' -> ' + ', '.join(f'{cid}{terms}' for cid, terms in hits.items())
            print(f'{question.id} [{question.category}] {question.question}{marker}')
        print()
    report = evaluate(KeywordRouter(), dataset, keep_cases=False)
    print('\n'.join(report.summary_lines()))
    print('\n'.join(report.category_lines()))
    print()
    if findings:
        print(f'FAIL: {len(findings)} finding(s)')
        for finding in findings:
            print('  -', finding)
        return 1
    seal_state = 'sealed' if dataset.seal else 'unsealed'
    print(f'OK: {dataset.name} ({seal_state}), {len(dataset.candidates)} candidates, '
          f'{len(dataset.questions)} questions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
