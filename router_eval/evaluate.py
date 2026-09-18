"""Run any router against the sealed question set and count the outcomes.

The point of this module is comparison over time, not a score for one model.
It takes a `Router` and a `Dataset` and returns counts; it contains no
provider-specific code, so the keyword baseline and a hosted router are both
passed to the same `evaluate(router, dataset)`.

What the router is given is exactly the contract: a `RouterInput` holding the
question, the optional previous question, and `CandidateView`s. The expected
labels never enter the request, and neither do the candidates' descriptions.

Outcomes are five mutually exclusive buckets:

    expected_hit     selected a candidate the label calls most desirable
    acceptable_hit   selected a candidate the label calls not wrong
    wrong_display    selected a candidate that is neither            <- the harmful case
    correct_abstain  selected nothing, and nothing was right
    missed_but_safe  selected nothing when a candidate was expected   <- a miss, but safe

`missed_but_safe` is preferred over `wrong_display`: a wrong candidate is worse
than no candidate at all. `wrong_display_rate` is therefore reported as the
primary figure rather than folded into an accuracy number.

Scores are not aggregated. A keyword match count and a model's own number are
different measurements, so the report records `score_type` and leaves the
numbers alone; there is deliberately no calibration or probability-quality
metric here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contract import RouterInput
from .dataset import CATEGORIES, load_dataset

OUTCOMES = ('expected_hit', 'acceptable_hit', 'wrong_display', 'correct_abstain', 'missed_but_safe')
PRIMARY_METRICS = ('wrong_display_rate', 'correct_abstain_rate', 'missed_but_safe_rate')
SECONDARY_METRICS = ('expected_hit_rate', 'acceptable_or_better_rate', 'abstain_precision',
                     'abstain_recall', 'false_positive_rate', 'false_negative_rate',
                     'multiple_candidate_recall')


def classify(question, result):
    """Which of the five buckets this result falls into."""
    if result.abstain or result.selected_id is None:
        return 'correct_abstain' if question.should_abstain else 'missed_but_safe'
    if result.selected_id in question.expected_ids:
        return 'expected_hit'
    if result.selected_id in question.acceptable_ids:
        return 'acceptable_hit'
    return 'wrong_display'


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def _rate(part, whole):
    return round(part / whole, 4) if whole else None


@dataclass
class Case:
    """One question's outcome, kept so a run can be inspected question by question."""
    question_id: str
    category: str
    outcome: str
    selected_id: object
    alternatives: tuple
    latency_ms: float


@dataclass
class Report:
    provider: str
    score_type: str
    dataset: str
    total: int
    outcomes: dict
    by_category: dict
    metrics: dict
    latency: dict
    cases: list = field(default_factory=list)

    def summary_lines(self):
        lines = [f'provider={self.provider}  score_type={self.score_type}  dataset={self.dataset}  n={self.total}']
        for name in OUTCOMES:
            count = self.outcomes[name]
            if self.total:
                lines.append(f'  {name:<16} {count:>4}  {count / self.total:6.1%}')
            else:
                lines.append(f'  {name}')
        lines.append('  --- primary ---')
        for key in PRIMARY_METRICS:
            lines.append(f'  {key:<26} {self.metrics[key]}')
        lines.append('  --- secondary ---')
        for key in SECONDARY_METRICS:
            lines.append(f'  {key:<26} {self.metrics[key]}')
        lines.append(f'  latency_ms p50={self.latency["p50"]} p95={self.latency["p95"]} max={self.latency["max"]}')
        return lines

    def category_lines(self):
        lines = ['  --- by category (expected/acceptable/wrong/abstain-ok/missed) ---']
        for name, counts in self.by_category.items():
            total = sum(counts.values())
            if not total:
                continue
            lines.append(f'  {name:<12} n={total:>3}  {counts["expected_hit"]:>3} {counts["acceptable_hit"]:>3} '
                         f'{counts["wrong_display"]:>3} {counts["correct_abstain"]:>3} {counts["missed_but_safe"]:>3}')
        return lines


def evaluate(router, dataset=None, keep_cases=True):
    """Run one router over the whole dataset. The router sees only the candidate views."""
    dataset = dataset if dataset is not None else load_dataset()
    outcomes = {name: 0 for name in OUTCOMES}
    by_category = {name: {outcome: 0 for outcome in OUTCOMES} for name in CATEGORIES}
    latencies = []
    cases = []
    score_types = set()
    abstained = 0
    should_abstain = 0
    with_expected = 0
    expected_hit = 0
    false_negative = 0
    multiple_recalls = []
    for question in dataset.questions:
        request = RouterInput(question=question.question, candidates=dataset.views,
                              previous_question=question.previous_question)
        result = router.route(request)
        score_types.add(result.score_type)
        outcome = classify(question, result)
        outcomes[outcome] += 1
        by_category[question.category][outcome] += 1
        latencies.append(result.latency_ms)
        if result.abstain:
            abstained += 1
        if question.should_abstain:
            should_abstain += 1
        if question.expected_ids:
            with_expected += 1
            if outcome == 'expected_hit':
                expected_hit += 1
            else:
                false_negative += 1
        if question.category == 'multiple' and question.expected_ids:
            offered = {result.selected_id} if result.selected_id else set()
            offered |= {candidate.candidate_id for candidate in result.alternatives}
            multiple_recalls.append(len(set(question.expected_ids) & offered) / len(question.expected_ids))
        if keep_cases:
            cases.append(Case(question_id=question.id, category=question.category, outcome=outcome,
                              selected_id=result.selected_id,
                              alternatives=tuple((item.candidate_id, item.score) for item in result.alternatives),
                              latency_ms=result.latency_ms))
    total = len(dataset.questions)
    metrics = {
        # Primary: a wrong candidate is worse than no candidate at all.
        'wrong_display_rate': _rate(outcomes['wrong_display'], total),
        'correct_abstain_rate': _rate(outcomes['correct_abstain'], should_abstain),
        'missed_but_safe_rate': _rate(outcomes['missed_but_safe'], total),
        # Secondary
        'expected_hit_rate': _rate(expected_hit, with_expected),
        'acceptable_or_better_rate': _rate(outcomes['expected_hit'] + outcomes['acceptable_hit'], total),
        'abstain_precision': _rate(outcomes['correct_abstain'], abstained),
        'abstain_recall': _rate(outcomes['correct_abstain'], should_abstain),
        'false_positive_rate': _rate(outcomes['wrong_display'], total),
        'false_negative_rate': _rate(false_negative, with_expected),
        'multiple_candidate_recall': (round(sum(multiple_recalls) / len(multiple_recalls), 4)
                                      if multiple_recalls else None),
        'counts': {'should_abstain': should_abstain, 'abstained': abstained, 'with_expected': with_expected,
                   'false_positive': outcomes['wrong_display'], 'false_negative': false_negative},
    }
    latency = {'p50': _percentile(latencies, 0.5), 'p95': _percentile(latencies, 0.95),
               'max': round(max(latencies), 3) if latencies else 0.0}
    return Report(provider=router.name, score_type='/'.join(sorted(score_types)), dataset=dataset.name,
                  total=total, outcomes=outcomes, by_category=by_category, metrics=metrics,
                  latency=latency, cases=cases)


def report_to_dict(report, include_cases=True):
    """Serialisable form. Contains only question ids and candidate ids, never text."""
    data = {'provider': report.provider, 'score_type': report.score_type, 'dataset': report.dataset,
            'total': report.total, 'outcomes': report.outcomes, 'by_category': report.by_category,
            'metrics': report.metrics, 'latency_ms': report.latency,
            'note': 'scores are provider-native and are not aggregated or treated as probabilities'}
    if include_cases:
        data['cases'] = [{'question_id': case.question_id, 'category': case.category, 'outcome': case.outcome,
                          'selected_id': case.selected_id,
                          'alternatives': [list(item) for item in case.alternatives],
                          'latency_ms': case.latency_ms} for case in report.cases]
    return data
