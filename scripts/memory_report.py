"""Aggregate paging and coexistence samples without exposing process identifiers.

    python scripts/memory_report.py --capture artifacts/capture-soak --output artifacts/memory-report.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

SYSTEM_KEYS = ('system_available_mib', 'system_commit_available_mib', 'system_cpu_pct', 'vram_mib')
APP_KEYS = ('rss_mib', 'private_mib', 'process_count')


def stats(values):
    return {'samples': len(values), 'min': min(values), 'max': max(values), 'mean': statistics.mean(values)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pages = {}
    apps = {}
    memory = {}
    samples = 0
    elapsed = 0
    with (args.capture / 'samples.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            samples += 1
            elapsed = row['elapsed_s']
            for key, value in row.get('paging', {}).items():
                if isinstance(value, (int, float)):
                    pages.setdefault(key, []).append(value)
            resource = row.get('resources', {})
            for key in SYSTEM_KEYS:
                if isinstance(resource.get(key), (int, float)):
                    memory.setdefault(key, []).append(resource[key])
            for name, data in row.get('apps', {}).items():
                for key in APP_KEYS:
                    apps.setdefault(name, {}).setdefault(key, []).append(data[key])
    report = {'elapsed_observed_s': elapsed, 'samples': samples,
              'paging': {key: stats(values) for key, values in pages.items()},
              'system': {key: stats(values) for key, values in memory.items()},
              'applications': {name: {key: stats(values) for key, values in fields.items()}
                               for name, fields in apps.items()},
              'scope': 'whole Windows PC during explicit test; not attributable to one process alone',
              'limitations': ['Pages Input/sec includes mapped files; it does not by itself prove swapping.',
                              'RSS sums can double-count shared pages; private commit is not resident RAM.',
                              'This observation includes model startup, review UI, browser/meeting apps '
                              'and other running apps.']}
    summary = args.capture / 'summary.json'
    if summary.exists():
        report['capture_summary'] = json.loads(summary.read_text(encoding='utf-8'))
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({'elapsed_observed_s': elapsed, 'samples': samples,
                      'physical_available_min_mib': report['system'].get('system_available_mib', {}).get('min'),
                      'commit_available_min_mib': report['system'].get('system_commit_available_mib', {}).get('min')}))


if __name__ == '__main__':
    main()
