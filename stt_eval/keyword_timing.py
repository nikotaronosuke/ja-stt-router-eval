"""Keyword-end latency, distinct from utterance-start keyword arrival.

`keyword_arrival_ms` (in `metrics`) measures from the start of the utterance and
therefore includes the time it takes to say the keyword. The figures here measure
from a human-verified annotation of where the keyword ends in the audio to the
first received event that contains it. Negative values mean the recognizer showed
the word before the annotated end; they are kept, not clipped.
"""
from __future__ import annotations

import math

from .metrics import NATURAL_SPEECH_SOURCE, hits, quantiles


def keyword_end_metrics(events, t0_ns, test, annotation, canonical_sha256, duration_s, end_times_ns=None):
    base = {'definition': 'first_received_match_minus_annotated_audio_end',
            'clock': 'host_perf_counter_scheduled_replay', 'annotation_status': 'missing',
            'items': [], 'latency_ms': quantiles([])}
    if not annotation:
        return base
    if end_times_ns is not None:
        base['clock'] = 'host_perf_counter_capture_block_timestamps'
    if annotation.get('canonical_sha256') != canonical_sha256:
        raise ValueError('annotation audio hash mismatch')
    seen = set()
    for span in annotation.get('spans', []):
        index = span['keyword_index']
        start, end = span['start_s'], span['end_s']
        valid_index = isinstance(index, int) and 0 <= index < len(test['keywords']) and index not in seen
        valid_numbers = all(isinstance(value, (int, float)) and math.isfinite(value) for value in [start, end])
        if not valid_index or not valid_numbers or not 0 <= start < end <= duration_s:
            raise ValueError('invalid keyword span')
        seen.add(index)
        # A repeated term needs occurrence-aware alignment, not substring matching.
        if span.get('occurrence', 1) != 1:
            raise ValueError('only unique first keyword occurrences are supported')
        if span.get('term') != test['keywords'][index]:
            raise ValueError('annotation term mismatch')
        verified = span.get('review') == 'human_verified'
        term = test['keywords'][index]
        if test.get('source') == NATURAL_SPEECH_SOURCE:
            from .spelling import aliases
            term = aliases(term)
        arrival = next((event for event in events if hits(event['text'], [term])[0]), None)
        elapsed = (arrival['t_ns'] - t0_ns) / 1e6 if arrival else None
        if end_times_ns is not None:
            end_ns = end_times_ns[index]
        else:
            end_ns = t0_ns + round(end * 1e9)
        latency = (arrival['t_ns'] - end_ns) / 1e6 if verified and arrival is not None else None
        base['items'].append({'keyword_index': index, 'review': span.get('review', 'unreviewed'),
                              'audio_end_s': end, 'arrival_ms': elapsed, 'latency_ms': latency,
                              'early_prediction': latency < 0 if latency is not None else None,
                              'arrival_was_final': arrival['final'] if arrival else None})
    verified_items = [item for item in base['items'] if item['review'] == 'human_verified']
    if verified_items and len(verified_items) == len(base['items']):
        base['annotation_status'] = 'human_verified'
    else:
        base['annotation_status'] = 'unreviewed_or_partial'
    base['latency_ms'] = quantiles([item['latency_ms'] for item in verified_items])
    base['unannotated_keyword_count'] = len(test['keywords']) - len(verified_items)
    return base


def captured_keyword_end_times(annotation, timeline):
    """Map canonical sample offsets to receipt time on the receiving Windows PC."""
    result = {}
    for span in annotation.get('spans', []):
        frame = span['end_s'] * 16000
        block = next((item for item in timeline if item['start_frame'] <= frame <= item['end_frame']), None)
        if block is None:
            raise ValueError('keyword end is outside the captured timeline')
        result[span['keyword_index']] = block['block_received_ns'] - round((block['end_frame'] - frame) / 16000 * 1e9)
    return result
