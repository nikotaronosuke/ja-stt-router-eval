"""One synthetic fixture: actual local prefix, injected mid-turn fault, hosted replay.

    python scripts/fallback_smoke.py --allow-cloud-fixtures --output artifacts/fallback-smoke

Sends at most one existing synthetic fixture (bounded to 31 s) to the hosted
engine, and only after the local engine has really received one second of it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.engines import OpenAIEngine, ParakeetEngine  # noqa: E402
from stt_eval.fallback import FallbackEngine  # noqa: E402
from stt_eval.harness import RunLock, compare  # noqa: E402
from stt_eval.safety import AudioBudget, DiagnosticError  # noqa: E402

FAULT_AFTER_FRAMES = 16000


class InjectedFault:
    def __init__(self, engine):
        self.engine = engine
        self.model = engine.model
        self.metadata = engine.metadata
        self.frames = 0

    async def open(self):
        await self.engine.open()

    async def begin(self, callback):
        await self.engine.begin(callback)

    async def feed(self, chunk):
        self.frames += len(chunk.pcm16) // 2
        if self.frames >= FAULT_AFTER_FRAMES:
            raise DiagnosticError('injected_local_fault')
        await self.engine.feed(chunk)

    async def finish(self):
        await self.engine.finish()

    async def abort(self):
        await self.engine.abort()

    async def close(self):
        await self.engine.close()


async def run(args):
    primary = ParakeetEngine(ROOT, interval_s=.3, inference_timeout_s=8.)
    secondary = OpenAIEngine(AudioBudget(ROOT / 'artifacts' / 'budget-ledger.json', 5),
                             allow_network=args.allow_cloud_fixtures, delay='medium', hints=True,
                             keywords=['天気予報', 'SharePoint'], prompt='公開用の汎用音声認識テスト。')
    adapter = FallbackEngine(InjectedFault(primary), secondary, allow_cloud_test_audio=args.allow_cloud_fixtures)
    result = await compare(ROOT / 'fixtures' / 'manifests' / 'synthetic-smoke.json', args.output, [adapter],
                           limit=1, purpose='injected_fault_actual_api')
    rows = [json.loads(line) for line in (args.output / 'results.jsonl').read_text(encoding='utf-8').splitlines()
            if line.strip()]
    final_received = len(rows) == 1 and rows[0]['status'] == 'ok' and bool(rows[0].get('final_text'))
    injected = len(adapter.turn_stats) == 1 and adapter.turn_stats[0].get('reason') == 'injected_local_fault'
    evidence = {'fault': 'injected_after_1s_local_input', 'actual_gpu_primary': injected,
                'actual_hosted_secondary': secondary.total_audio_s > 0, 'turns': adapter.turn_stats,
                'final_received': final_received, 'hosted_audio_s': secondary.total_audio_s,
                'run_error': result.get('fatal_error'),
                'transmission_scope': 'one_existing_synthetic_fixture_only', 'reserved_audio_s': 31.}
    first = adapter.turn_stats[0] if adapter.turn_stats else {}
    passed = (injected and final_received and 0 < secondary.total_audio_s <= 31 and first.get('switched')
              and first.get('cloud_frames') == first.get('source_frames') and not result.get('fatal_error'))
    evidence['status'] = 'PASS' if passed else 'FAIL'
    (args.output / 'fallback.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps({'status': evidence['status'], 'audio_s': secondary.total_audio_s}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-cloud-fixtures', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    if not arguments.allow_cloud_fixtures:
        parser.error('explicit cloud fixture approval required')
    with RunLock(ROOT):
        asyncio.run(run(arguments))
