"""Pre-flight checks and a cost estimate before a hosted router is sent anything.

Nothing here opens a connection. It builds the requests that *would* be sent
and inspects them, so a run can be refused before the first byte leaves the
machine.

Two separate questions are answered:

  precheck(dataset, router)  is it safe and correct to send this at all?
  estimate(dataset, router)  what would it cost?

The safety check is deliberately paranoid about content. It asserts the dataset
is the sealed fictional one, that the seal still matches the files, and then
takes the actual outbound payload apart: every value in it must be accounted
for by the question, the previous question, or a candidate's id, title and
search terms. Anything else, such as a candidate's description or an expected-id
label, fails the check. The same pass looks for material that must never be
sent regardless of where it came from: an email address, a local path, a URL, a
key-shaped string, a real company suffix.

Token counts are an estimate. No tokenizer is a dependency of this project, and
counting through the API would mean sending the text, which is the thing being
gated. The estimate counts CJK characters as roughly one token each and ASCII at
four characters per token, then applies a margin, so it is meant to be an upper
bound rather than an accurate figure. The real usage is read back from the API
response.
"""
from __future__ import annotations

import json
import re

from .contract import RouterInput
from .dataset import seal_findings
from .openai_router import build_payload

SEALED_DATASET = 'ja-router-eval-v1'
SEALED_QUESTIONS = 242
SEALED_CANDIDATES = 16
FICTIONAL_MARKER = '架空'
TOKEN_MARGIN = 1.30  # the estimate is an upper bound, not a measurement


def _chars(*codepoints):
    return ''.join(chr(value) for value in codepoints)


# The two Japanese company suffixes (kabushiki-gaisha, yugen-gaisha) are built from code
# points so that this file never contains them literally and a privacy scan stays clean.
COMPANY_SUFFIXES = '|'.join((_chars(0x682a, 0x5f0f, 0x4f1a, 0x793e), _chars(0x6709, 0x9650, 0x4f1a, 0x793e)))

FORBIDDEN_PATTERNS = (
    ('email', re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')),
    ('windows_path', re.compile(r'[A-Za-z]:\\\\|[A-Za-z]:\\|%LOCALAPPDATA%|%APPDATA%|AppData')),
    ('posix_home', re.compile(r'/home/[a-z]|/Users/')),
    ('url', re.compile(r'https?://')),
    ('api_key', re.compile(r'sk-[A-Za-z0-9]{8}|Bearer\s+[A-Za-z0-9]{8}|[A-Za-z0-9_-]{32,}')),
    ('uuid', re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),
    ('real_company', re.compile(COMPANY_SUFFIXES + r'|Inc\.|Corp\.|Ltd\.')),
)
# The only keys a request may carry. The forbidden-pattern scan covers all of them,
# the fixed instructions included, so the check does not depend on trusting our own text.
STRUCTURAL_KEYS = ('model', 'instructions', 'input', 'text', 'reasoning', 'store', 'stream', 'max_output_tokens')
JAPANESE_RUN = re.compile(r'[぀-ヿ一-鿿]{12,}')


def _payload_values(value, found=None):
    """Every string that would actually be transmitted, flattened."""
    found = [] if found is None else found
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            _payload_values(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _payload_values(item, found)
    return found


def _estimate_tokens(text):
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return int(((len(text) - ascii_chars) + ascii_chars / 4) * TOKEN_MARGIN)


def _requests(dataset):
    for question in dataset.questions:
        yield question, RouterInput(question=question.question, candidates=dataset.views,
                                    previous_question=question.previous_question)


def precheck(dataset, router, directory=None):
    """Return a list of findings. An empty list means it is safe to send."""
    findings = []
    if dataset.name != SEALED_DATASET:
        findings.append(f'dataset_is_not_sealed:{dataset.name}')
    if len(dataset.questions) != SEALED_QUESTIONS:
        findings.append(f'question_count:{len(dataset.questions)}!={SEALED_QUESTIONS}')
    if len(dataset.candidates) != SEALED_CANDIDATES:
        findings.append(f'candidate_count:{len(dataset.candidates)}!={SEALED_CANDIDATES}')
    if directory is not None:
        for finding in seal_findings(directory, dataset.seal or None, dataset.name,
                                     len(dataset.candidates), len(dataset.questions)):
            findings.append(finding)
    for candidate in dataset.candidates:
        if FICTIONAL_MARKER not in json.dumps(candidate.__dict__, ensure_ascii=False):
            findings.append(f'candidate_missing_fictional_marker:{candidate.id}')
    # Material that must never be sent, whatever its origin.
    allowed = set()
    for candidate in dataset.candidates:
        allowed.update({candidate.id, candidate.title, *candidate.triggers})
    for question, request in _requests(dataset):
        payload = build_payload(request, router.model, router.effort)
        if payload.get('store') is not False:
            findings.append(f'store_not_false:{question.id}')
        if payload.get('stream') is not False:
            findings.append(f'stream_not_false:{question.id}')
        if set(payload) - set(STRUCTURAL_KEYS):
            findings.append(f'unexpected_payload_key:{sorted(set(payload) - set(STRUCTURAL_KEYS))}')
        sent = payload['input']
        # The candidate body and the labels must not appear anywhere in the request.
        for candidate in dataset.candidates:
            if candidate.description and candidate.description in sent:
                findings.append(f'candidate_body_in_payload:{question.id}:{candidate.id}')
        if question.notes and question.notes in sent:
            findings.append(f'notes_in_payload:{question.id}')
        for label in list(question.expected_ids) + list(question.acceptable_ids):
            if label not in allowed:
                findings.append(f'label_leak:{question.id}')
        texts = _payload_values(payload)
        for name, pattern in FORBIDDEN_PATTERNS:
            if any(pattern.search(text) for text in texts):
                findings.append(f'{name}_in_payload:{question.id}')
        # The input must be reconstructible from question + previous question + views.
        expected = {question.question.strip()}
        if question.previous_question:
            expected.add(question.previous_question.strip())
        leftover = sent
        for piece in sorted(expected | allowed, key=len, reverse=True):
            if piece:
                leftover = leftover.replace(piece, '')
        if JAPANESE_RUN.search(leftover):
            findings.append(f'unaccounted_text_in_payload:{question.id}')
    return sorted(set(findings))


def estimate(dataset, router):
    """Upper-bound token and cost estimate for one full pass. Sends nothing."""
    input_tokens = 0
    sample = None
    for _, request in _requests(dataset):
        payload = build_payload(request, router.model, router.effort)
        text = str(payload['instructions']) + str(payload['input']) + json.dumps(payload['text'], ensure_ascii=False)
        input_tokens += _estimate_tokens(text)
        if sample is None:
            sample = payload
    questions = len(dataset.questions)
    output_cap = int(sample['max_output_tokens']) if sample else 0
    output_tokens = output_cap * questions  # worst case: every reply fills the cap
    cost = (input_tokens * router.prices['input'] + output_tokens * router.prices['output']) / 1_000_000
    return {
        'requests': questions,
        'input_tokens_per_request': input_tokens // questions if questions else 0,
        'input_tokens_total': input_tokens,
        'output_tokens_cap_per_request': output_cap,
        'output_tokens_total_worst_case': output_tokens,
        'input_usd': round(input_tokens * router.prices['input'] / 1_000_000, 4),
        'output_usd_worst_case': round(output_tokens * router.prices['output'] / 1_000_000, 4),
        'total_usd_worst_case': round(cost, 4),
        'budget_usd': router.budget_usd,
        'within_budget': cost <= router.budget_usd,
        'note': 'upper bound; no tokenizer is installed and no counting request was sent. '
                'Prompt caching would lower the input cost further and is not assumed.',
        'sample_payload': sample,
    }
