"""Explicit spelling equivalence for the natural-speech evaluation.

Only the aliases listed here are treated as the same term. There is no fuzzy or
semantic matching: CER and strict keyword recall are unchanged, and a separate
spelling-equivalent recall accepts these aliases alone.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .metrics import hits, normalize

ALIASES = {'SharePoint': ['SharePoint', 'シェアポイント']}


def aliases(term):
    return ALIASES.get(term, [term])


def occurrences(text, term):
    """Normalized (start, end) spans of every alias of `term` in `text`."""
    text = normalize(text)
    matches = set()
    for alias in aliases(term):
        value = normalize(alias)
        start = 0
        while value:
            position = text.find(value, start)
            if position < 0:
                break
            matches.add((position, position + len(value)))
            start = position + len(value)
    return sorted(matches)


def equivalence_scores(test, final_text):
    """Separate from strict spelling and CER; no fuzzy or semantic substitutions."""
    if test.get('reference_review') != 'verified' or final_text is None:
        return {'keyword_equivalent_hit': None, 'proper_noun_equivalent_hit': None}

    def score(terms):
        return [hits(final_text, [aliases(term)])[0] for term in terms
                if occurrences(test['reference_text'], term)]

    return {'keyword_equivalent_hit': score(test['keywords']),
            'proper_noun_equivalent_hit': score(test['proper_nouns'])}


def timing_draft(test, draft):
    """Navigation aid only: a person must hear and confirm, including the alignment."""
    if test.get('reference_review') != 'verified':
        return None
    term = test['keywords'][0]
    target = occurrences(test['reference_text'], term)
    if len(target) != 1:
        return None
    chars = draft.get('chars', [])
    text = ''
    mapping = []
    for item in chars:
        token = normalize(item['text'])
        text += token
        mapping.extend([item] * len(token))
    if not mapping:
        return None
    matches = occurrences(text, term)
    method = 'model_timestamp'
    if len(matches) == 1:
        left, right = matches[0]
    elif len(matches) > 1:
        return None
    else:
        method = 'approximate_text_alignment'
        left, right = target[0]
        positions = []
        reference = normalize(test['reference_text'])
        for tag, a, b, c, d in SequenceMatcher(None, reference, text, autojunk=False).get_opcodes():
            if b <= left or a >= right:
                continue
            if tag == 'equal':
                positions.extend(range(c + max(a, left) - a, c + min(b, right) - a))
            elif tag == 'replace':
                positions.extend(range(c, d))
        if not positions:
            return None
        left, right = min(positions), max(positions) + 1
    start, end = mapping[left]['start_s'], mapping[right - 1]['end_s']
    if not 0 <= start < end <= test['duration_s']:
        return None
    return {'start_s': start, 'end_s': end, 'term': term, 'review': 'model_estimate_only', 'method': method}
