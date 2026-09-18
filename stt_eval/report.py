"""Per-run CSV, JSON and Markdown summaries for the comparison harness."""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from .metrics import quantiles
from .safety import PRICE_USD_PER_MINUTE, PRICE_VERIFIED_ON


def write_csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text('', encoding='utf-8-sig')
        return
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            values = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                      for key, value in row.items()}
            # CSV may be opened in a spreadsheet. Text must never be interpreted as a formula.
            protected = {}
            for key, value in values.items():
                if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                    protected[key] = "'" + value
                else:
                    protected[key] = value
            writer.writerow(protected)


def summarize(rows: list[dict]):
    groups = defaultdict(list)
    for row in rows:
        key = (row['engine'], row['condition'], row['audio_kind'], str(row['hints']))
        groups[key].append(row)
    result = []
    for (engine, condition, audio_kind, hints), group in groups.items():
        valid = [row for row in group if row.get('status') == 'ok']
        entry = {'engine': engine, 'condition': condition, 'audio_kind': audio_kind, 'hints': hints,
                 'total': len(group), 'completed': len(valid), 'errors': len(group) - len(valid)}
        for metric in ['cer', 'keyword_recall', 'proper_noun_accuracy']:
            values = [row[metric] for row in valid if row.get(metric) is not None]
            entry[metric] = statistics.mean(values) if values else None
            entry[metric + '_n'] = len(values)
        for metric, hit_field in [('keyword_recall', 'keyword_hit'), ('proper_noun_accuracy', 'proper_noun_hits')]:
            flags = [hit for row in valid for hit in (row.get(hit_field) or [])]
            entry[metric + '_macro'] = entry[metric]
            entry[metric] = sum(flags) / len(flags) if flags else None
            entry[metric + '_term_count'] = len(flags)
        for metric in ['first_partial_ms', 'keyword_arrival_ms', 'stable_ms']:
            entry[metric] = quantiles([row.get(metric) if row.get('status') == 'ok' else None for row in group])
        result.append(entry)
    return result


def _format(value, percent=False):
    if value is None:
        return '未計測'
    return f'{value * 100:.1f}%' if percent else f'{value:.1f}'


def write_report(directory: Path, rows: list[dict], run: dict):
    write_csv(directory / 'results.csv', rows)
    groups = summarize(rows)
    (directory / 'summary.json').write_text(json.dumps({'run': run, 'groups': groups}, indent=2, ensure_ascii=False),
                                            encoding='utf-8')
    lines = ['# STT 計測結果', '', f"試験種別: {run.get('purpose', '未記録')}",
             f"実経過時間: {run.get('elapsed_s', 0):.1f}秒", '',
             '合成音声の結果は接続・計測の検証専用。実話者の日本語品質の根拠にはしない。',
             'CERはサンプル平均、語彙指標は語数によるmicro平均。失敗と欠測は別件数。', '',
             '|Engine|条件|音声種別|hint|成功/全件|CER|Keyword Recall|固有名詞|Keyword P95 ms|到達/全件|',
             '|---|---|---|---|---:|---:|---:|---:|---:|---:|']
    for group in groups:
        arrival = group['keyword_arrival_ms']
        lines.append(f"|{group['engine']}|{group['condition']}|{group['audio_kind']}|{group['hints']}|"
                     f"{group['completed']}/{group['total']}|{_format(group['cer'], True)}|"
                     f"{_format(group['keyword_recall'], True)}|{_format(group['proper_noun_accuracy'], True)}|"
                     f"{_format(arrival['p95'])}|{arrival['count']}/{arrival['total']}|")
    lines += ['', '## 遅延', '', '単位ms。P50/P90/P95/maxはnearest-rank。欠測は0に置換しない。', '',
              '|Engine/条件|指標|P50|P90|P95|max|取得/全件|', '|---|---|---:|---:|---:|---:|---:|']
    for group in groups:
        for metric in ['first_partial_ms', 'keyword_arrival_ms', 'stable_ms']:
            values = group[metric]
            lines.append(f"|{group['engine']}/{group['condition']}|{metric}|"
                         + '|'.join(_format(values[key]) for key in ['p50', 'p90', 'p95', 'max'])
                         + f"|{values['count']}/{values['total']}|")
    lines += ['', '## PC負荷', '',
              'ホストプロセスCPUは全論理CPUで正規化。GPU/VRAMはGPU全体。WSL worker CPUは1コア=100%の別カウンター。', '',
              '|項目|平均|最大|', '|---|---:|---:|']
    resources = run.get('resources', {})
    for key in ['app_cpu_pct', 'system_cpu_pct', 'app_ram_mib', 'system_ram_mib', 'gpu_pct', 'vram_mib',
                'worker_cpu_pct', 'worker_ram_mib', 'cuda_allocated_mib', 'cuda_reserved_mib']:
        lines.append(f"|{key}|{_format(resources.get(key + '_avg'))}|{_format(resources.get(key + '_max'))}|")
    memory_change = json.dumps(run.get('memory_change', {}), ensure_ascii=False)
    lines += ['', '## 連続動作', '',
              f"60分試験: {run.get('soak_status', '未計測')}",
              f"接続数: {run.get('openai_connections', 0)} / 計画ローテーション: {run.get('openai_rotations', 0)}",
              f"送信音声: {run.get('openai_audio_s', 0):.2f}秒 / 接続: {run.get('openai_connection_s', 0):.2f}秒",
              f"エラー件数: {sum(row.get('status') != 'ok' for row in rows)}",
              f"初期化エラー: {json.dumps(run.get('model_setup_errors', []), ensure_ascii=False)}",
              f"実行中断理由: {run.get('fatal_error', 'なし')}",
              'メモリ増加: ' + memory_change + ' MiB（中央値差、漏洩の断定ではない）',
              '', '## 費用', '',
              f'公式確認済み単価: hosted transcription USD {PRICE_USD_PER_MINUTE}/音声分（{PRICE_VERIFIED_ON}）。',
              f"このrunの推定費用: USD {run.get('openai_audio_s', 0) / 60 * PRICE_USD_PER_MINUTE:.5f}。請求確定額ではない。",
              'ローカル推論: API従量課金なし。電力・機器費用は未計測。', '',
              '## 品質判定と未計測', '',
              '実話者の小声・早口・言い淀み・会議codec・周辺雑音は、該当音源と人手レビューが揃うまで未計測。',
              '意味を壊す誤認識はmeaning-review.csvに件数を記入して別途レビューする。自動推定しない。', '',
              '設定・モデル・hash・キュー滞留はrun.json / results.jsonl、partialはevents.jsonl、負荷はresources.jsonlを参照。']
    (directory / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    review_path = directory / 'meaning-review.csv'
    if not review_path.exists():
        columns = ['trial', 'engine', 'test_id', 'reference_text', 'final_text', 'meaning_error_count',
                   'meaning_review_status']
        write_csv(review_path, [{key: row.get(key) for key in columns} for row in rows])
