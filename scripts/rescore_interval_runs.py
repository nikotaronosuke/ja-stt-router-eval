"""Rescore immutable interval-sweep traces with the current human-reviewed references.

    python scripts/rescore_interval_runs.py --baseline artifacts/interval-sweep \
        --remaining artifacts/interval-remaining --references fixtures/manifests/natural-speech.json \
        --output artifacts/interval-rescored.json

The saved runs are never modified. Every unique recording is rescored against the
reviewed reference text and the human-verified keyword boundary, so figures written
while references were still drafts are replaced rather than reused.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.keyword_timing import captured_keyword_end_times, keyword_end_metrics  # noqa: E402
from stt_eval.metrics import (TranscriptTrace, edit_distance, guard_reviewed_keyword_reference,  # noqa: E402
                              normalize, quantiles)
from stt_eval.spelling import equivalence_scores  # noqa: E402

INTERVALS_MS = (500, 300, 250, 200)
QUALITY_KEYS = ('cer', 'keyword_hit', 'keyword_recall', 'proper_noun_hits', 'proper_noun_accuracy')
MEMORY_KEYS = ('system_available_mib', 'system_commit_available_mib', 'wsl_swap_used_mib', 'wsl_oom_kill_count')
NOTES = ['Quality uses first trial per unique recording, not repeated stability trials.',
         'Public speech includes transformed versions of the same original utterances; they are not '
         'statistically independent speakers/samples.',
         'First partial is from recording/replay start; leading human pauses are included.',
         'Keyword end uses only human-verified boundaries; model drafts never substitute for truth.',
         'Spelling-equivalent keyword recall accepts only the explicit alias table. CER and strict recall are unchanged.',
         'Keyword end uses the same spelling equivalence for natural speech; repeated reference terms are '
         'excluded from boundary review.',
         'Meeting route is participant-attested; measured clock starts on the receiver PC, not the remote microphone.']


def read_jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def summarize(rows):
    valid = [row for row in rows if row.get('status') == 'ok']
    reviewed = [row for row in valid if row.get('reference_review') in ['verified', 'dataset_reference']]
    flags = [value for row in reviewed for value in (row.get('keyword_hit') or [])]
    equivalent = [value for row in reviewed for value in (row.get('keyword_equivalent_hit') or [])]
    proper = [value for row in reviewed for value in (row.get('proper_noun_equivalent_hit') or [])]
    length = sum(len(normalize(row['reference_text'])) for row in reviewed)
    errors = sum(edit_distance(normalize(row['reference_text']), normalize(row['final_text'] or '')) for row in reviewed)
    timings = [item.get('latency_ms') for row in rows for item in row.get('keyword_end', {}).get('items', [])
               if item.get('review') == 'human_verified']
    cers = [row['cer'] for row in reviewed if row.get('cer') is not None]
    return {'total': len(rows), 'successful': len(valid), 'failed': len(rows) - len(valid),
            'reviewed': len(reviewed),
            'asr_assisted_reference_count': sum(row.get('reference_origin') == 'human_review_of_asr_draft'
                                                for row in reviewed),
            'cer_micro': errors / length if length else None, 'reference_characters': length,
            'cer_macro': statistics.mean(cers) if cers else None,
            'keyword_recall': sum(flags) / len(flags) if flags else None, 'keyword_count': len(flags),
            'keyword_recall_spelling_equivalent': sum(equivalent) / len(equivalent) if equivalent else None,
            'keyword_equivalent_count': len(equivalent),
            'proper_noun_recall_spelling_equivalent': sum(proper) / len(proper) if proper else None,
            'proper_noun_equivalent_count': len(proper),
            'first_partial_ms': quantiles([row.get('first_partial_ms') if row.get('status') == 'ok' else None
                                           for row in rows]),
            'keyword_end_ms': quantiles(timings), 'human_annotated_keywords': len(timings),
            'keyword_early_prediction_count': sum(value is not None and value < 0 for value in timings),
            'partial_revisions': quantiles([row.get('partial_revision_count') for row in valid]),
            'queue_max_ms': max((row.get('queue_max_ms', 0) for row in rows), default=None)}


def rescore(row, events, test):
    if row['canonical_sha256'] != test['canonical_sha256']:
        raise ValueError('reference_audio_mismatch')
    metrics = TranscriptTrace(row['t0_ns'], events).result(test['reference_text'], test['keywords'],
                                                           test['proper_nouns'], test['decision_keywords'])
    if test.get('reference_review') != 'verified':
        for key in QUALITY_KEYS:
            metrics[key] = None
    guard_reviewed_keyword_reference(metrics, test)
    return {**row, **metrics, 'reference_text': test['reference_text'],
            **equivalence_scores(test, metrics['final_text']), 'route': test.get('route'),
            'reference_review': test['reference_review'], 'reference_origin': test.get('reference_origin'),
            'keyword_end': keyword_end_metrics(events, row['t0_ns'], test, test['keyword_annotation'],
                                               row['canonical_sha256'], test['duration_s'])}


def by_key(rows, key):
    return {value: summarize([row for row in rows if row.get(key) == value])
            for value in sorted({row[key] for row in rows if row.get(key)})}


def interval_run(path, references):
    path = path.resolve()
    if not (path / 'run.json').is_file():
        return None
    run = json.loads((path / 'run.json').read_text(encoding='utf-8'))
    raw = read_jsonl(path / 'results.jsonl')
    events = {}
    event_rows = read_jsonl(path / 'events.jsonl')
    inferences = [event for event in event_rows if event.get('kind') == 'inference']
    for event in event_rows:
        if event.get('kind') == 'transcript':
            events.setdefault(event['trial'], []).append(event)
    unique = {}
    for row in raw:
        if row['test_id'] in unique:
            continue
        if row['test_id'] in references:
            row = rescore(row, events.get(row['trial'], []), references[row['test_id']])
        unique[row['test_id']] = row
    rows = list(unique.values())
    resource = read_jsonl(path / 'resources.jsonl')
    extra = {}
    for key in MEMORY_KEYS:
        values = [row[key] for row in resource if isinstance(row.get(key), (int, float))]
        extra[key] = {'min': min(values), 'max': max(values)} if values else None
    start_gaps = [(b['started_ns'] - a['started_ns']) / 1e6 for a, b in zip(inferences, inferences[1:])
                  if b['audio_s'] > a['audio_s']]
    return {'path': str(path.relative_to(ROOT)).replace('\\', '/'), 'elapsed_s': run['elapsed_s'],
            'stability_status': run['stability_status'], 'trial_count': len(raw),
            'all_trials_successful': bool(raw) and all(row['status'] == 'ok' for row in raw),
            'inference_ms': quantiles([event['inference_ms'] for event in inferences]),
            'inference_start_gap_ms': quantiles(start_gaps),
            'unique_recordings': summarize(rows),
            'by_condition': by_key(rows, 'condition'), 'by_route': by_key(rows, 'route'),
            'by_audio_kind_condition': {
                kind: {condition: summarize([row for row in rows if row.get('audio_kind') == kind
                                             and row['condition'] == condition])
                       for condition in sorted({row['condition'] for row in rows if row.get('audio_kind') == kind})}
                for kind in sorted({row['audio_kind'] for row in rows if row.get('audio_kind')})},
            'by_route_condition': {
                route: {condition: summarize([row for row in rows if row.get('route') == route
                                              and row['condition'] == condition])
                        for condition in sorted({row['condition'] for row in rows if row.get('route') == route})}
                for route in sorted({row['route'] for row in rows if row.get('route')})},
            'resources': run['resources'], 'memory': extra}


def meeting_rows(references):
    meetings = []
    capture_by_route = {}
    for test in references.values():
        if not test.get('live_trace'):
            continue
        trace = json.loads((ROOT / test['live_trace']).read_text(encoding='utf-8'))
        base = {'test_id': test['test_id'], 'status': 'ok', 't0_ns': trace['t0_ns'],
                'canonical_sha256': trace['canonical_sha256'], 'queue_max_ms': trace['queue_max_ms']}
        row = rescore(base, trace['events'], test)
        row['route'] = test['route']
        row['keyword_end'] = keyword_end_metrics(
            trace['events'], trace['t0_ns'], test, test['keyword_annotation'], trace['canonical_sha256'],
            test['duration_s'], captured_keyword_end_times(test['keyword_annotation'], trace['timeline']))
        meetings.append(row)
        capture = test.get('capture_summary')
        record = capture_by_route.setdefault(test['route'], {
            'clips': 0, 'summary_missing': 0, 'capture_errors': 0, 'status_flags': 0,
            'queue_dropped_frames': 0, 'max_callback_gap_ms': 0., 'max_asr_queue_ms': 0.})
        record['clips'] += 1
        record['max_asr_queue_ms'] = max(record['max_asr_queue_ms'], trace['queue_max_ms'])
        if not capture:
            record['summary_missing'] += 1
            continue
        record['capture_errors'] += len(capture['errors'])
        for stream in capture['streams'].values():
            record['status_flags'] |= stream['status_flags']
            record['queue_dropped_frames'] += stream['queue_dropped_frames']
            record['max_callback_gap_ms'] = max(record['max_callback_gap_ms'], stream.get('max_callback_gap_ms', 0))
    return meetings, capture_by_route


def main(args):
    manifest = args.references.resolve()
    if manifest.exists():
        references = {test['test_id']: test for test in json.loads(manifest.read_text(encoding='utf-8'))['tests']}
    else:
        references = {}
    intervals = {}
    for ms in INTERVALS_MS:
        folder = args.baseline if ms == 500 else args.remaining
        if folder:
            record = interval_run(folder / f'interval-{ms}ms', references)
            if record:
                intervals[str(ms)] = record
    natural = {}
    if args.remaining:
        for ms in INTERVALS_MS:
            record = interval_run(args.remaining / f'natural-{ms}ms', references)
            if record:
                natural[str(ms)] = record
    meetings, capture_by_route = meeting_rows(references)
    timing_verified = sum(span['review'] == 'human_verified' for test in references.values()
                          for span in test['keyword_annotation']['spans'])
    output = {'intervals': intervals, 'natural_same_audio': natural,
              'meetings': {route: summarize([row for row in meetings if row['route'] == route])
                           for route in ['meet_loopback', 'zoom_loopback']},
              'meeting_capture': capture_by_route,
              'review_counts': {'recorded': len(references),
                                'text_verified': sum(test['reference_review'] == 'verified' for test in references.values()),
                                'timing_verified': timing_verified},
              'notes': NOTES}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'completed_intervals': sum(record['stability_status'] == 'PASS' for record in intervals.values()),
                      'review_counts': output['review_counts']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, default=ROOT / 'artifacts' / 'interval-sweep')
    parser.add_argument('--remaining', type=Path)
    parser.add_argument('--references', type=Path, default=ROOT / 'fixtures' / 'manifests' / 'natural-speech.json')
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
