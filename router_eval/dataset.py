"""The sealed evaluation set: candidates, labelled questions and the seal.

A dataset directory holds three files:

    candidates.json   the closed set of candidate ids with title, triggers and a
                      display-only description
    questions.json    labelled questions with expected / acceptable ids and notes
    seal.json         SHA-256 of the two files above, written before any provider
                      comparison

Loading verifies the seal by default. An edit to either fixture file therefore
fails loudly until `scripts/seal_router_fixture.py` is run again, which is the
reminder that a changed dataset is a new dataset and earlier results no longer
compare to it.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

CATEGORIES = ('direct', 'paraphrase', 'indirect', 'follow_up', 'multiple', 'none', 'ambiguous',
              'distractor', 'scope_trap', 'very_short')
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / 'fixtures' / 'router-eval-v1'
SEAL_FILE = 'seal.json'
SEALED_FILES = ('candidates.json', 'questions.json')
ID_PATTERN = re.compile(r'[a-z][a-z0-9_-]{0,31}')
LIMITS = {'title': 80, 'trigger': 40, 'description': 400, 'triggers': 20}


def normalize_term(text):
    """NFKC, casefold, collapsed whitespace: the rule the keyword router applies."""
    return ' '.join(unicodedata.normalize('NFKC', str(text)).casefold().split())


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str
    triggers: tuple
    description: str


@dataclass(frozen=True)
class Question:
    id: str
    category: str
    question: str
    expected_ids: tuple
    acceptable_ids: tuple
    should_abstain: bool
    notes: str
    previous_question: str = ''


@dataclass(frozen=True)
class Dataset:
    name: str
    candidates: tuple
    views: tuple
    questions: tuple
    seal: dict

    def candidate(self, candidate_id):
        return next((item for item in self.candidates if item.id == candidate_id), None)


def validate_candidate(entry):
    if not isinstance(entry, dict):
        raise ValueError('invalid_candidate')
    candidate_id = str(entry.get('id', ''))
    if not ID_PATTERN.fullmatch(candidate_id):
        raise ValueError('invalid_candidate_id:' + candidate_id)
    title = ' '.join(str(entry.get('title', '')).split())
    if not title:
        raise ValueError('title_required:' + candidate_id)
    if len(title) > LIMITS['title']:
        raise ValueError('title_too_long:' + candidate_id)
    raw = entry.get('triggers')
    if not isinstance(raw, list):
        raise ValueError('invalid_triggers:' + candidate_id)
    triggers = []
    for term in raw:
        term = normalize_term(term)
        if not term:
            continue
        if len(term) > LIMITS['trigger']:
            raise ValueError('trigger_too_long:' + candidate_id)
        if term not in triggers:
            triggers.append(term)
    if not triggers:
        raise ValueError('trigger_required:' + candidate_id)
    if len(triggers) > LIMITS['triggers']:
        raise ValueError('too_many_triggers:' + candidate_id)
    description = ' '.join(str(entry.get('description', '') or '').split())
    if len(description) > LIMITS['description']:
        raise ValueError('description_too_long:' + candidate_id)
    return Candidate(id=candidate_id, title=title, triggers=tuple(triggers), description=description)


def read_seal(directory):
    path = Path(directory) / SEAL_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def seal_findings(directory, seal, name, candidate_count, question_count):
    """Why a directory does not match its seal. Empty means sealed and unchanged."""
    directory = Path(directory)
    if seal is None:
        return ['seal_missing']
    findings = []
    if seal.get('dataset') != name:
        findings.append('seal_dataset_name_mismatch')
    if seal.get('candidates') != candidate_count:
        findings.append('seal_candidate_count_mismatch')
    if seal.get('questions') != question_count:
        findings.append('seal_question_count_mismatch')
    recorded = seal.get('files') or {}
    for filename in SEALED_FILES:
        if recorded.get(filename) != file_digest(directory / filename):
            findings.append('seal_mismatch:' + filename)
    return findings


def load_dataset(directory=None, *, require_seal=True):
    """Read and validate a dataset directory.

    With `require_seal` (the default) the seal must exist and match the files.
    Pass `require_seal=False` only while authoring a fixture.
    """
    directory = Path(directory) if directory is not None else DEFAULT_DIR
    raw_candidates = json.loads((directory / 'candidates.json').read_text(encoding='utf-8'))
    raw_questions = json.loads((directory / 'questions.json').read_text(encoding='utf-8'))
    name = str(raw_candidates.get('dataset', 'router-eval'))
    if raw_questions.get('dataset', name) != name:
        raise ValueError('dataset_name_mismatch')
    candidates = []
    for entry in raw_candidates['candidates']:
        candidate = validate_candidate(entry)
        if any(item.id == candidate.id for item in candidates):
            raise ValueError('duplicate_candidate_id:' + candidate.id)
        candidates.append(candidate)
    ids = {candidate.id for candidate in candidates}
    questions = []
    seen = set()
    for entry in raw_questions['questions']:
        question_id = str(entry['id'])
        if question_id in seen:
            raise ValueError('duplicate_question_id:' + question_id)
        seen.add(question_id)
        expected = tuple(entry.get('expected_ids') or ())
        acceptable = tuple(entry.get('acceptable_ids') or ())
        unknown = [item for item in expected + acceptable if item not in ids]
        if unknown:
            raise ValueError(f'unknown_candidate_in_labels:{question_id}:{unknown}')
        if entry['category'] not in CATEGORIES:
            raise ValueError('unknown_category:' + question_id)
        questions.append(Question(id=question_id, category=str(entry['category']),
                                  question=str(entry['question']), expected_ids=expected,
                                  acceptable_ids=acceptable, should_abstain=bool(entry['should_abstain']),
                                  notes=str(entry.get('notes', '')),
                                  previous_question=str(entry.get('previous_question', '') or '')))
    seal = read_seal(directory)
    if require_seal:
        findings = seal_findings(directory, seal, name, len(candidates), len(questions))
        if findings:
            raise ValueError('dataset_not_sealed:' + ','.join(findings))
    from .contract import CandidateView
    return Dataset(name=name, candidates=tuple(candidates), views=CandidateView.many(candidates),
                   questions=tuple(questions), seal=dict(seal or {}))
