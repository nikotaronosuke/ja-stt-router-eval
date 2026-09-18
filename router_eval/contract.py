"""Router contract: choose one candidate id for a short utterance, or abstain.

A router answers exactly one question: "which candidate, if any, does this
utterance point at?" It never produces text for a person to read. That is a
property of the contract rather than of any prompt: `RouterResult` is a frozen
dataclass with a closed field list, so there is nowhere to put a sentence.
Adding such a field would be a change to this file, which the contract tests
guard.

What a router receives is also closed. `CandidateView` carries the id, the
title and the search terms of a candidate. Any display-only body text never
enters a router, which is what lets a hosted provider run somewhere else while
the body stays local.

Scores are provider-native. A keyword count and a hosted model's self-reported
relevance are different measurements, so every result states its `score_type`
and scores are only ever compared within one type (`comparable`). No router's
number is treated as "the probability that this is the right candidate".

Abstaining is a correct answer. When nothing matches well enough, the router
returns `selected_id=None` with `abstain=True`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, fields

# Field names a router must never be able to return. Checked against the contract
# itself, so a future provider cannot smuggle generated prose through metadata.
FORBIDDEN_FIELDS = frozenset({
    'answer', 'answers', 'suggested_answer', 'suggestion', 'generated_response',
    'generated_text', 'recommended_script', 'script', 'reply', 'response',
    'completion', 'draft', 'rewrite', 'say', 'speech', 'utterance', 'sentence',
    'paraphrase', 'summary', 'advice', 'tip', 'hint_text',
})
METADATA_VALUE_MAX = 80
METADATA_MAX_KEYS = 12


def _check_metadata(value):
    """Diagnostics only: short scalars under non-answer keys. Never body text."""
    if not isinstance(value, dict):
        raise ValueError('invalid_metadata')
    if len(value) > METADATA_MAX_KEYS:
        raise ValueError('metadata_too_large')
    cleaned = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError('invalid_metadata_key')
        if key.casefold() in FORBIDDEN_FIELDS:
            raise ValueError('forbidden_metadata_key:' + key)
        if type(item) in (bool, int, float):
            cleaned[key] = item
            continue
        if not isinstance(item, str):
            raise ValueError('invalid_metadata_value:' + key)
        if len(item) > METADATA_VALUE_MAX:
            raise ValueError('metadata_value_too_long:' + key)
        cleaned[key] = item
    return cleaned


@dataclass(frozen=True)
class CandidateView:
    """Everything a router is allowed to know about one candidate.

    `title` and `triggers` are the candidate's own search handles. The body
    (`description` in the fixture) is display data and stays with the display.
    """
    id: str
    title: str
    triggers: tuple = ()

    @classmethod
    def of(cls, candidate):
        if isinstance(candidate, dict):
            triggers = candidate.get('triggers') or ()
            return cls(id=str(candidate.get('id', '')), title=str(candidate.get('title', '')),
                       triggers=tuple(str(term) for term in triggers))
        return cls(id=str(candidate.id), title=str(candidate.title),
                   triggers=tuple(str(term) for term in candidate.triggers))

    @classmethod
    def many(cls, candidates):
        return tuple(cls.of(candidate) for candidate in candidates)


@dataclass(frozen=True)
class RouterInput:
    """The utterance plus the candidates to choose between.

    `previous_question` is optional context for a follow-up such as 「具体的には？」.
    A router may ignore it.
    """
    question: str
    candidates: tuple = ()
    previous_question: str = ''


@dataclass(frozen=True)
class ScoredCandidate:
    """One candidate the router considered, with the provider's own number.

    `score` is only meaningful next to the result's `score_type`.
    """
    candidate_id: str
    score: float


@dataclass(frozen=True)
class RouterResult:
    """The whole of what a router may return. No field carries text to be read."""
    provider: str
    score_type: str
    selected_id: object = None
    alternatives: tuple = ()
    abstain: bool = True
    latency_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.selected_id is not None and not isinstance(self.selected_id, str):
            raise ValueError('invalid_selected_id')
        if (self.selected_id is None) != bool(self.abstain):
            raise ValueError('abstain_must_match_selection')
        object.__setattr__(self, 'alternatives', tuple(self.alternatives))
        object.__setattr__(self, 'metadata', _check_metadata(self.metadata))


def contract_violations(record=RouterResult):
    """Field names on the result type that would let a router hand over an answer."""
    return sorted(item.name for item in fields(record) if item.name.casefold() in FORBIDDEN_FIELDS)


def comparable(first, second):
    """Two results' scores may be compared only when they measure the same thing."""
    return first.score_type == second.score_type


class Router:
    """Base class for every provider: keyword, a hosted API, or a later one.

    Subclasses set `name`, `score_type` and `decision_policy`, optionally
    `threshold`, and implement `decide`. `route` only adds the timing and builds
    the result, so no provider can forget to report which kind of score it produced.
    """
    name = 'router'
    score_type = 'none'
    decision_policy = 'not specified'
    threshold = None

    def decide(self, request):
        """Return (selected_id or None, [ScoredCandidate, ...], metadata dict)."""
        raise NotImplementedError

    def route(self, request):
        started = time.perf_counter()
        selected, candidates, metadata = self.decide(request)
        elapsed = (time.perf_counter() - started) * 1000
        return RouterResult(provider=self.name, score_type=self.score_type, selected_id=selected,
                            alternatives=tuple(candidates), abstain=selected is None,
                            latency_ms=round(elapsed, 3), metadata=dict(metadata or {}))
