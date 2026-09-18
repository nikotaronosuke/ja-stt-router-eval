"""KeywordRouter: the reference provider.

Score type is `trigger_match_count`: how many of the candidate's own search
terms occur in the utterance. It is a count, not a probability, and it is not
comparable with another provider's number.

Decision policy: a candidate matches when any of its terms appears in the
NFKC-casefolded utterance; exactly one matching candidate is selected, and zero
matches or two-or-more matches both abstain. Ambiguity abstains because showing
the wrong candidate is worse than showing none.
"""
from __future__ import annotations

import unicodedata

from .contract import Router, ScoredCandidate

MAX_UTTERANCE_CHARS = 2000


def normalize(text):
    """NFKC, casefold, collapsed whitespace: the same rule the fixture applies to terms."""
    return ' '.join(unicodedata.normalize('NFKC', str(text)).casefold().split())


class KeywordRouter(Router):
    name = 'keyword'
    score_type = 'trigger_match_count'
    decision_policy = 'exactly one candidate whose trigger occurs in the utterance; 0 or 2+ abstain'
    threshold = None

    def decide(self, request):
        text = normalize(str(request.question or '')[:MAX_UTTERANCE_CHARS])
        candidates = []
        for view in request.candidates:
            hits = sum(1 for term in view.triggers if term and normalize(term) in text)
            if hits:
                candidates.append(ScoredCandidate(candidate_id=view.id, score=float(hits)))
        candidates.sort(key=lambda item: (-item.score, item.candidate_id))
        selected = candidates[0].candidate_id if len(candidates) == 1 else None
        return selected, candidates, {'matched': len(candidates), 'considered': len(request.candidates)}
