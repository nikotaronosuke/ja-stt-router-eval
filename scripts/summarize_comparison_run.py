"""Aggregate an observed comparison run; never substitute absent tests with estimates.

    python scripts/summarize_comparison_run.py --run artifacts/comparison-soak-65min \
        --manifest fixtures/manifests/comparison-160.json --output artifacts/comparison-analysis.json \
        --markdown artifacts/comparison-report.md

Quality figures use only the first trial of each fixture; repeated utterances in a
long run are stability evidence, not new accuracy samples. Optional inputs add the
capture soak, the live loopback smoke, the meaning review table, per-setting runs
and high-delay runs to the same report.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.metrics import hits, normalize  # noqa: E402
from stt_eval.report import summarize  # noqa: E402

ENGINES = ('openai', 'parakeet')
RESOURCE_KEYS = ('app_cpu_pct', 'system_cpu_pct', 'app_ram_mib', 'system_ram_mib', 'system_available_mib',
                 'gpu_pct', 'vram_mib', 'gpu_temp_c', 'worker_cpu_pct', 'worker_ram_mib', 'cuda_allocated_mib',
                 'cuda_reserved_mib')
STARTUP_KEYS = ('system_ram_mib', 'system_available_mib', 'app_ram_mib', 'worker_ram_mib', 'cuda_allocated_mib',
                'cuda_reserved_mib', 'vram_mib')
CYCLE_KEYS = ('app_ram_mib', 'worker_ram_mib', 'cuda_allocated_mib', 'cuda_reserved_mib')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8')) if path and path.exists() else None


def read_lines(path):
    rows = []
    if path and path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                break  # an active writer may have an incomplete final line
    return rows


def pct(value):
    return '未計測' if value is None else f'{value * 100:.1f}%'


def num(value):
    return '未計測' if value is None else f'{value:.1f}'


def parse_labelled(value):
    label, _, path = value.partition('=')
    if not label or not path:
        raise argparse.ArgumentTypeError('expected LABEL=PATH')
    return label, Path(path)


def capture_validation(capture):
    if not capture:
        return None
    coverage = {name: stream['frames'] / (capture['formats'][name]['rate'] * capture['duration_s'])
                for name, stream in capture['streams'].items()}
    clean = not capture['errors'] and all(stream['status_flags'] == 0 and stream['queue_dropped_frames'] == 0
                                          for stream in capture['streams'].values())
    render_clean = capture.get('silent_render_stats', {}).get('status_flags', 0) == 0
    passed = (clean and capture['duration_s'] >= 3600 and all(.999 <= value <= 1.001 for value in coverage.values())
              and render_clean)
    if passed:
        status = 'PASS'
    elif clean and not capture.get('silent_render_active'):
        status = 'INCONCLUSIVE (uncontrolled silence)'
    else:
        status = 'FAIL'
    return {'frame_coverage_ratio': coverage, 'pass': passed, 'status': status}


def analyse(args):
    rows = read_lines(args.run / 'results.jsonl')
    run = read_json(args.run / 'run.json')
    # Repeated utterances in the soak are not new independent quality samples.
    first = {}
    for row in rows:
        first.setdefault((row['engine'], row['test_id']), row)
    first_rows = list(first.values())
    groups = summarize(first_rows)
    repeatability = {}
    for engine in ENGINES:
        repeats = [row for row in rows if row['engine'] == engine and row['status'] == 'ok'
                   and (engine, row['test_id']) in first
                   and row['trial'] != first[(engine, row['test_id'])]['trial']
                   and first[(engine, row['test_id'])]['status'] == 'ok']
        same = sum(normalize(row['final_text']) == normalize(first[(engine, row['test_id'])]['final_text'])
                   for row in repeats)
        change = statistics.mean(row['cer'] - first[(engine, row['test_id'])]['cer'] for row in repeats) if repeats else None
        repeatability[engine] = {'repeated_trials': len(repeats), 'same_normalized_final': same,
                                 'mean_cer_change_from_first': change}
    manifest = read_json(args.manifest)
    terms = {test['test_id']: test['decision_keywords'] for test in manifest['tests']}
    first_test_id = manifest['tests'][0]['test_id']
    partial_quality = []
    for group in groups:
        subset = [row for row in first_rows if row['engine'] == group['engine']
                  and row['condition'] == group['condition'] and row['status'] == 'ok']
        revisions = [row['partial_revision_count'] for row in subset]
        retracted = sum(row['keyword_arrival_ms'] is not None and not all(hits(row['final_text'], terms[row['test_id']]))
                        for row in subset)
        partial_quality.append({'engine': group['engine'], 'condition': group['condition'],
                                'revision_mean': statistics.mean(revisions) if revisions else None,
                                'revision_max': max(revisions, default=None),
                                'keyword_retracted_samples': retracted, 'n': len(subset)})
    by_trial = {}
    for row in rows:
        by_trial.setdefault(row['trial'], {})[row['engine']] = row
    pairs = [pair for pair in by_trial.values() if len(pair) == 2]
    identical = bool(pairs) and all(pair['openai']['canonical_sha256'] == pair['parakeet']['canonical_sha256']
                                    and pair['openai']['audio_frames'] == pair['parakeet']['audio_frames']
                                    for pair in pairs)
    counters = read_lines(args.run / 'resources.jsonl')
    lower = min((row['t0_ns'] for row in rows), default=0)
    upper = max((row['t0_ns'] + int(row['final_ms'] * 1e6) for row in rows if row.get('final_ms') is not None),
                default=0)
    runtime = [row for row in counters if lower <= row['t_ns'] <= upper]
    setup = [row for row in counters if row['t_ns'] < lower]
    startup_resources = {}
    for key in STARTUP_KEYS:
        values = [row[key] for row in setup if row.get(key) is not None]
        startup_resources[key] = {'max': max(values, default=None), 'min': min(values, default=None)}
    resources = {}
    for key in RESOURCE_KEYS:
        values = [row[key] for row in runtime if row.get(key) is not None]
        drift = statistics.median(values[-60:]) - statistics.median(values[:60]) if len(values) >= 120 else None
        resources[key] = {'count': len(values), 'mean': statistics.mean(values) if values else None,
                          'max': max(values, default=None), 'min': min(values, default=None),
                          'last60_minus_first60': drift}
    capture = read_json(args.capture)
    cycle_markers = []
    for row in rows:
        if row['engine'] != 'parakeet' or row['test_id'] != first_test_id or row.get('final_ms') is None:
            continue
        window = [item for item in counters if row['t0_ns'] <= item['t_ns'] <= row['t0_ns'] + int(row['final_ms'] * 1e6)]
        marker = {'elapsed_min': row['completed_elapsed_s'] / 60}
        for key in CYCLE_KEYS:
            values = [item[key] for item in window if item.get(key) is not None]
            marker[key] = statistics.median(values) if values else None
        cycle_markers.append(marker)
    ledger = read_json(args.ledger)
    high_rows = []
    high_run = None
    high_interruption = None
    for folder in args.high_run:
        high_rows += read_lines(folder / 'results.jsonl')
        high_run = read_json(folder / 'run.json') or high_run
        high_interruption = read_json(folder / 'interruption.json') or high_interruption
    high_complete = (bool(high_run) and len({row['test_id'] for row in high_rows if row['status'] == 'ok'}) == 80
                     and all(row['status'] == 'ok' for row in high_rows))
    high_groups = summarize(high_rows)
    high_hash_match = bool(high_rows) and all(
        (row['engine'], row['test_id']) in first
        and row['canonical_sha256'] == first[(row['engine'], row['test_id'])]['canonical_sha256']
        and row['audio_frames'] == first[(row['engine'], row['test_id'])]['audio_frames'] for row in high_rows)
    live_check = read_json(args.live_loopback)
    review = read_json(args.review)
    review_groups = {}
    if review:
        cases = {case['case']: case for case in review['cases']}
        for sample in review['samples']:
            setting = '/high' if sample.get('configuration') == 'high' else ''
            key = sample['engine'] + setting + '/' + sample['condition']
            counts = review_groups.setdefault(key, {'reviewed': 0, 'meaning_breaking_utterances': 0, 'pending': 0})
            value = cases[sample['case']]['meaning_breaking_utterance']
            if value is None:
                counts['pending'] += 1
            else:
                counts['reviewed'] += 1
                counts['meaning_breaking_utterances'] += value
    settings = {}
    for label, path in args.settings_run:
        summary = read_json(path / 'summary.json')
        if summary:
            settings[label] = summary['groups'][0]
    if run and run.get('soak_status') == 'PASS':
        state = '完走'
    elif run:
        state = '終了・失敗あり'
    else:
        state = '実行中（中間集計）'
    queue_max = {engine: max((row['queue_max_ms'] for row in rows if row['engine'] == engine), default=None)
                 for engine in ENGINES}
    return {'state': state, 'unique_quality_rows': len(first_rows), 'paired_trials': len(pairs),
            'identical_audio': identical, 'groups': groups, 'partial_quality': partial_quality,
            'repeatability': repeatability, 'resources_runtime_only': resources,
            'resources_startup_only': startup_resources, 'run': run,
            'all_trial_errors': sum(row['status'] != 'ok' for row in rows), 'queue_max_ms': queue_max,
            'capture': capture, 'capture_validation': capture_validation(capture), 'live_loopback': live_check,
            'settings_runs': settings,
            'high_delay': {'continuation_run': high_run, 'groups': high_groups,
                           'original_interruption': high_interruption, 'complete_80_tests': high_complete,
                           'same_input_as_medium': high_hash_match},
            'resource_counter_errors': sum(len(row.get('counter_errors', [])) for row in runtime),
            'text_review': review_groups, 'same_fixture_memory_by_cycle': cycle_markers,
            'cumulative_estimated_usd': ledger.get('estimated_usd') if ledger else None}


def markdown(stats):
    now = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M JST')
    run = stats['run'] or {}
    lines = ['# STT 同一入力比較 — 集計結果', '', f"更新: {now}。継続試験: {stats['state']}。", '',
             '各音源の最初の試行のみで品質を集計する。長時間試験の同じ音源の反復を独立サンプルとして水増ししない。',
             'CERはサンプル平均、語彙はmicro平均。T0は発話全体の投入開始で、キーワードが発話されるまでの時間も含む。', '',
             '## 同一入力での認識品質・遅延', '',
             '|条件|方式|成功/件数|CER|Keyword Recall|固有名詞|First Partial P95 ms|Keyword P95 ms|Keyword到達/件数|',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for group in stats['groups']:
        arrival = group['keyword_arrival_ms']
        lines.append(f"|{group['condition']}|{group['engine']}|{group['completed']}/{group['total']}|{pct(group['cer'])}|"
                     f"{pct(group['keyword_recall'])}|{pct(group['proper_noun_accuracy'])}|"
                     f"{num(group['first_partial_ms']['p95'])}|{num(arrival['p95'])}|{arrival['count']}/{arrival['total']}|")
    lines += ['', f"両方式の音声hash・フレーム数一致: {stats['identical_audio']}（{stats['paired_trials']}組）。", '',
              '## 遅延の分布（初回の各音源）', '',
              '単位ms。nearest-rank。First Partialは文字/数字を含む最初の非finalであり意味的な有用性は保証しない。', '',
              '|方式/条件|指標|P50|P90|P95|最大|到達/全件|', '|---|---|---:|---:|---:|---:|---:|']
    for group in stats['groups']:
        for metric in ['first_partial_ms', 'keyword_arrival_ms', 'stable_ms']:
            values = group[metric]
            lines.append(f"|{group['engine']}/{group['condition']}|{metric}|"
                         + '|'.join(num(values[key]) for key in ['p50', 'p90', 'p95', 'max'])
                         + f"|{values['count']}/{values['total']}|")
    lines += ['', '## partialの修正とキーワードの維持', '',
              '|方式/条件|修正回数 平均|最大|途中では正しく出た判定語がfinalで失われた発話|件数|', '|---|---:|---:|---:|---:|']
    for item in stats['partial_quality']:
        lines.append(f"|{item['engine']}/{item['condition']}|{num(item['revision_mean'])}|{num(item['revision_max'])}|"
                     f"{item['keyword_retracted_samples']}|{item['n']}|")
    if stats['settings_runs']:
        lines += ['', '## 設定比較（別run）', '',
                  '|設定|成功|CER|Keyword Recall|固有名詞|First Partial P95 ms|Keyword P95 ms|',
                  '|---|---:|---:|---:|---:|---:|---:|']
        for label, group in stats['settings_runs'].items():
            lines.append(f"|{label}|{group['completed']}/{group['total']}|{pct(group['cer'])}|{pct(group['keyword_recall'])}|"
                         f"{pct(group['proper_noun_accuracy'])}|{num(group['first_partial_ms']['p95'])}|"
                         f"{num(group['keyword_arrival_ms']['p95'])}|")
    high = stats['high_delay']
    if high['groups']:
        lines += ['', '## 高精度設定の追加比較', '',
                  '|条件|設定|成功/件数|CER|Keyword Recall|固有名詞|First Partial P95 ms|Keyword P95 ms|到達/件数|',
                  '|---|---|---:|---:|---:|---:|---:|---:|---:|']
        for group in high['groups']:
            arrival = group['keyword_arrival_ms']
            lines.append(f"|{group['condition']}|high|{group['completed']}/{group['total']}|{pct(group['cer'])}|"
                         f"{pct(group['keyword_recall'])}|{pct(group['proper_noun_accuracy'])}|"
                         f"{num(group['first_partial_ms']['p95'])}|{num(arrival['p95'])}|{arrival['count']}/{arrival['total']}|")
        lines.append(f"同一入力との音声hash・フレーム数一致: {high['same_input_as_medium']}。"
                     f"80件完了: {high['complete_80_tests']}。")
    if stats['text_review']:
        lines += ['', '## 意味の変化のテキストレビュー', '',
                  '正解文とfinalを照合した補助評価。録音の独立聴取は含まない。曖昧な表記は未判定。', '',
                  '|方式/条件|意味変化ありの発話|判定済み|未判定|', '|---|---:|---:|---:|']
        for key, count in stats['text_review'].items():
            lines.append(f"|{key}|{count['meaning_breaking_utterances']}|{count['reviewed']}|{count['pending']}|")
    lines += ['', '## 負荷（モデル起動後・同時比較中）', '',
              'ホストCPUは全論理CPU基準。WSL worker CPUは1コア100%。システムとGPU全体の値には他のアプリも含む。', '',
              '|指標|平均|最大|最小|終盤60点−序盤60点の中央値差|', '|---|---:|---:|---:|---:|']
    for key, value in stats['resources_runtime_only'].items():
        lines.append(f"|{key}|{num(value['mean'])}|{num(value['max'])}|{num(value['min'])}|{num(value['last60_minus_first60'])}|")
    startup = stats['resources_startup_only']
    lines += ['', f"カウンター取得エラー: {stats['resource_counter_errors']}件。",
              f"起動中システムRAM最大 {num(startup['system_ram_mib']['max'])}MiB / 空き最小 "
              f"{num(startup['system_available_mib']['min'])}MiB。", '',
              '同じ先頭fixtureに戻った時点のメモリ中央値（音声長の違いによるキャッシュ増加と区別する参考値）。', '',
              '|経過分|ホストMiB|worker MiB|CUDA allocated MiB|CUDA reserved MiB|', '|---:|---:|---:|---:|---:|']
    for marker in stats['same_fixture_memory_by_cycle']:
        lines.append('|' + '|'.join(num(marker[key]) for key in ('elapsed_min', *CYCLE_KEYS)) + '|')
    lines += ['', '## 継続試験', '', f"状態: {stats['state']}。全試行エラー {stats['all_trial_errors']}件。"]
    if run:
        lines += [f"実時間 {run.get('elapsed_s', 0):.1f}秒 / 判定 {run.get('soak_status')}。",
                  f"最終partialの到着経過秒: {json.dumps(run.get('last_partial_turn_elapsed_s'), ensure_ascii=False)}。",
                  f"hosted接続 {run.get('openai_connections', 0)}回 / 計画切替 {run.get('openai_rotations', 0)}回。",
                  f"初期化時間 {run.get('load_s', 0):.1f}秒（STT時間に含めない）。"]
    lines.append(f"最大キュー滞留 ms: {json.dumps(stats['queue_max_ms'])}。")
    for engine, entry in stats['repeatability'].items():
        change = entry['mean_cer_change_from_first']
        lines.append(f"{engine}: 同一入力の再試行で初回とfinalが正規化一致 {entry['same_normalized_final']}/"
                     f"{entry['repeated_trials']}件。初回からの平均CER変化 "
                     f"{num(change * 100 if change is not None else None)}ポイント。")
    capture = stats['capture']
    validation = stats['capture_validation']
    if capture:
        lines += [f"WASAPI＋マイク独立耐久: {capture['duration_s']:.0f}秒。",
                  f"取得検査: {validation['status']}。フレーム時間の被覆率: {json.dumps(validation['frame_coverage_ratio'])}。",
                  f"無音render維持: {capture.get('silent_render_active', False)}。",
                  f"取得エラー数 {len(capture['errors'])}。"]
        for name, stream in capture['streams'].items():
            lines.append(f"{name}: frames={stream['frames']}, status_flags={stream['status_flags']}, "
                         f"queue_dropped_frames={stream['queue_dropped_frames']}。")
    live = stats['live_loopback']
    if live:
        lines += ['', '既定出力で既知fixtureを再生→WASAPI→ローカル認識の実経路:',
                  f"成功: {live.get('success')} / 判定語を含むpartial {live.get('partial_matches')}回 / "
                  f"final {live.get('final_matches')}回 / 最初の該当partial {num(live.get('first_matching_partial_ms'))}ms。",
                  'この値は再生API呼出から受信まで。fixture直接投入のT0とは基準が異なる。']
    lines += ['', '## 費用', '',
              f"累積予約ベース推定: USD {stats['cumulative_estimated_usd'] or 0:.4f}（失敗分も保守的に予約、請求確定額ではない）。",
              'ローカル推論: API従量課金なし。電力・機器費用は計測していない。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description='Aggregate a comparison run')
    parser.add_argument('--run', type=Path, required=True, help='run folder with results.jsonl, run.json, resources.jsonl')
    parser.add_argument('--manifest', type=Path, required=True, help='manifest that provides decision keywords')
    parser.add_argument('--capture', type=Path, help='capture-check JSON of the independent capture soak')
    parser.add_argument('--live-loopback', type=Path, help='live loopback smoke JSON')
    parser.add_argument('--review', type=Path, help='meaning-review JSON')
    parser.add_argument('--settings-run', type=parse_labelled, action='append', default=[],
                        help='LABEL=folder of a per-setting run with summary.json')
    parser.add_argument('--high-run', type=Path, action='append', default=[], help='folder of a high-delay run')
    parser.add_argument('--ledger', type=Path, help='budget ledger JSON')
    parser.add_argument('--output', type=Path, required=True, help='analysis JSON to write')
    parser.add_argument('--markdown', type=Path, help='optional Markdown report to write')
    args = parser.parse_args()
    stats = analyse(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.markdown:
        args.markdown.write_text(markdown(stats), encoding='utf-8')
    print(json.dumps({'state': stats['state'], 'paired_trials': stats['paired_trials'],
                      'unique_quality_rows': stats['unique_quality_rows'], 'errors': stats['all_trial_errors']}))


if __name__ == '__main__':
    main()
