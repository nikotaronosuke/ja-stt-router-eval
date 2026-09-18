"""Local model-assisted review drafts, never accepted as human ground truth.

A draft holds the recognizer's text and, when the decoder can produce them,
character timestamps. Both only help a person navigate the recording; the
reference text and the keyword boundary become annotations only after the person
confirms them in the review page.
"""
from __future__ import annotations

import json

from .audio import load_fixture, validate_manifest
from .metrics import normalize
from .safety import safe_error


def keyword_span(chars, keyword, duration_s):
    joined = ''
    mapping = []
    for item in chars:
        token = normalize(item['text'])
        joined += token
        mapping.extend([item] * len(token))
    term = normalize(keyword)
    if not term or joined.count(term) != 1:
        return None
    start = joined.index(term)
    begin = mapping[start]['start_s']
    end = mapping[start + len(term) - 1]['end_s']
    if not 0 <= begin < end <= duration_s:
        return None
    return {'start_s': begin, 'end_s': end, 'term': keyword, 'review': 'model_estimate_only'}


async def generate_drafts(engine, manifest, directory, progress=None):
    directory.mkdir(parents=True, exist_ok=True)
    generated = 0
    for test in validate_manifest(manifest):
        path = directory / (test['test_id'] + '.json')
        if path.exists():
            continue
        pcm, metadata = load_fixture(test['_audio_path'])
        if metadata['canonical_sha256'] != test['canonical_sha256']:
            raise ValueError('review_draft_audio_mismatch')
        try:
            draft = await engine.draft_timestamps(pcm)
        except Exception as error:
            # Keep text review useful when this decoder cannot emit timestamps.
            text, _ = await engine._infer(pcm)
            draft = {'text': text, 'chars': [], 'review': 'model_estimate_only',
                     'timestamp_error': safe_error(error)}
        draft.update(canonical_sha256=metadata['canonical_sha256'],
                     keyword_span=keyword_span(draft['chars'], test['keywords'][0], metadata['duration_s']),
                     source='offline_timestamp_navigation_aid', reference_origin='asr_assisted')
        with path.open('x', encoding='utf-8') as stream:
            json.dump(draft, stream, ensure_ascii=False)
        generated += 1
        if progress:
            progress(generated)
    return generated
