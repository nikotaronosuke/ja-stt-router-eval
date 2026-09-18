"""Play one fixture into the real WASAPI loopback and the local engine; retain no audio or text.

    python scripts/live_loopback_smoke.py

The first synthetic fixture is played through the default output once the separate
microphone stream confirms capture is running. Only match counts and the delay from
the playback call to the first matching partial are saved.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.engines import ParakeetEngine  # noqa: E402
from stt_eval.harness import RunLock  # noqa: E402
from stt_eval.live import listen  # noqa: E402
from stt_eval.metrics import normalize  # noqa: E402

MANIFEST = ROOT / 'fixtures' / 'manifests' / 'synthetic-smoke.json'
DESTINATION = ROOT / 'artifacts' / 'live-loopback-smoke.json'
LISTEN_SECONDS = 12


async def main():
    if DESTINATION.exists():
        raise ValueError('Existing result preserved')
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    stop = threading.Event()
    state = {'partial_matches': 0, 'final_matches': 0, 'first_matching_partial_ms': None}
    playback_ns = None
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    first = manifest['tests'][0]
    keyword = first['keywords'][0]
    if isinstance(keyword, list):
        keyword = keyword[0]
    keyword = normalize(keyword)
    fixture = (MANIFEST.parent / first['audio']).resolve()

    def emit(event):
        # A stopped render engine produces no loopback packets. The separately
        # opened microphone confirms capture is ready even before playback.
        if event.get('kind') == 'meter' and event.get('stream') == 'microphone':
            loop.call_soon_threadsafe(ready.set)
        if event.get('kind') == 'transcript' and keyword in normalize(event['text']):
            field = 'final_matches' if event.get('final') else 'partial_matches'
            state[field] += 1
            if not event.get('final') and playback_ns is not None and state['first_matching_partial_ms'] is None:
                state['first_matching_partial_ms'] = (time.perf_counter_ns() - playback_ns) / 1e6

    async def play():
        nonlocal playback_ns
        import winsound
        await ready.wait()
        await asyncio.sleep(.2)
        playback_ns = time.perf_counter_ns()
        winsound.PlaySound(str(fixture), winsound.SND_FILENAME | winsound.SND_ASYNC)

    player = asyncio.create_task(play())
    try:
        with RunLock(ROOT):
            summary = await listen([ParakeetEngine(ROOT)], stop, emit, seconds=LISTEN_SECONDS)
    finally:
        player.cancel()
        await asyncio.gather(player, return_exceptions=True)
    summary.update(state)
    summary['success'] = bool(state['partial_matches'] and state['final_matches'] and not summary['capture']['errors'])
    summary['notes'] = ('Known fixture playback; default loopback plus separate mic, local engine only; '
                        'no captured audio or transcripts persisted.')
    DESTINATION.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'success': summary['success'], 'partial_matches': state['partial_matches'],
                      'capture_errors': len(summary['capture']['errors'])}))


if __name__ == '__main__':
    asyncio.run(main())
