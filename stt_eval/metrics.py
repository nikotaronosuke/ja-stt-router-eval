"""Text normalization, CER, keyword hits and the per-utterance transcript trace.

Definitions (also in docs/methodology.md):

- CER: character Levenshtein distance after NFKC, casefold, and removal of
  whitespace and punctuation. No semantic normalization of numerals or spellings.
- Keyword recall: a term counts as hit when any of its listed spellings occurs in
  the normalized final text. Terms may be given as a string or a list of aliases.
- First partial: the first non-final event containing a letter or digit. Absent
  partials are `None`, never substituted with the final time.
- Keyword arrival: the first event in which all decision keywords are present.
- Stable: the first event from which the normalized text no longer changes.
- Quantiles: nearest-rank; missing values are counted, never treated as zero.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field

# Manifest `source` value for clips a person recorded following a prompt. Such a
# prompt is not evidence that the requested word was actually spoken.
NATURAL_SPEECH_SOURCE = 'elicited_natural_speech'


def normalize(text: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKC', text).casefold()
                   if not char.isspace() and not unicodedata.category(char).startswith(('P', 'Z')))


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float | None:
    reference, hypothesis = normalize(reference), normalize(hypothesis)
    return edit_distance(reference, hypothesis) / len(reference) if reference else None


def hits(text: str, terms: list) -> list[bool]:
    text = normalize(text)
    result = []
    for term in terms:
        aliases = term if isinstance(term, list) else [term]
        result.append(any(normalize(alias) in text for alias in aliases if normalize(alias)))
    return result


def ratio(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def quantiles(values: list[float | None]) -> dict:
    samples = sorted(value for value in values if value is not None and math.isfinite(value))
    result = {'count': len(samples), 'total': len(values), 'missing': len(values) - len(samples)}
    for name, fraction in [('p50', .50), ('p90', .90), ('p95', .95), ('max', 1.)]:
        result[name] = samples[max(0, math.ceil(fraction * len(samples)) - 1)] if samples else None
    return result


def guard_reviewed_keyword_reference(metrics, test):
    """An elicitation prompt is not evidence that its requested word was spoken."""
    if test.get('source') != NATURAL_SPEECH_SOURCE:
        return metrics
    verified = test.get('reference_review') == 'verified'
    present = hits(test['reference_text'], test['keywords']) if verified else []
    metrics['keyword_reference_status'] = 'present' if present and all(present) else 'unverified_or_absent'
    if not present or not all(present):
        for key in ['keyword_hit', 'keyword_recall', 'proper_noun_hits', 'proper_noun_accuracy']:
            metrics[key] = None
    return metrics


@dataclass
class TranscriptTrace:
    t0_ns: int
    events: list[dict] = field(default_factory=list)

    def add(self, text: str, received_ns: int, final: bool = False):
        if received_ns < self.t0_ns:
            raise ValueError('event predates T0')
        if self.events and received_ns < self.events[-1]['t_ns']:
            raise ValueError('events are not monotonic')
        self.events.append({'text': text, 't_ns': received_ns, 'final': final})

    def result(self, reference: str, keywords: list, proper_nouns: list,
               decision_keywords: list | None = None) -> dict:
        finals = [event for event in self.events if event['final']]
        final_text = finals[-1]['text'] if finals else None
        required = keywords if decision_keywords is None else decision_keywords
        first = next((event for event in self.events if not event['final']
                      and any(char.isalnum() for char in normalize(event['text']))), None)
        keyword = next((event for event in self.events if required and all(hits(event['text'], required))), None)
        stable = None
        if finals:
            final_normal = normalize(final_text)
            for event in reversed(self.events):
                if normalize(event['text']) != final_normal:
                    break
                stable = event
        revisions = 0
        old = ''
        for event in self.events:
            new = normalize(event['text'])
            if old and new != old and not new.startswith(old):
                revisions += 1
            old = new

        def elapsed(event):
            return (event['t_ns'] - self.t0_ns) / 1e6 if event else None

        keyword_hits = hits(final_text or '', keywords) if finals else None
        proper_hits = hits(final_text or '', proper_nouns) if finals else None
        return {
            'final_text': final_text, 'first_partial_ms': elapsed(first),
            'keyword_arrival_ms': elapsed(keyword), 'stable_ms': elapsed(stable),
            'final_ms': elapsed(finals[-1]) if finals else None,
            'cer': cer(reference, final_text) if finals else None,
            'keyword_hit': keyword_hits, 'keyword_recall': ratio(keyword_hits) if finals else None,
            'proper_noun_hits': proper_hits, 'proper_noun_accuracy': ratio(proper_hits) if finals else None,
            'partial_revision_count': revisions,
            'partial_event_count': sum(not event['final'] for event in self.events),
            'meaning_error_count': None, 'meaning_review_status': 'unreviewed',
        }
