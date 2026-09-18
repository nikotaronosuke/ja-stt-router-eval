import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
plan = json.loads((root/'fixtures/test-plan.json').read_text(encoding='utf-8'))
tests = []
for test in plan['tests']:
    audio = root/'fixtures/audio/synthetic'/f"{test['test_id']}.wav"
    if audio.exists():
        tests.append({**test, 'audio_kind':'synthetic_smoke', 'condition':'tts_normal',
                      'audio':f"../audio/synthetic/{test['test_id']}.wav"})
target = root/'fixtures/manifests/synthetic-smoke.json'
target.parent.mkdir(exist_ok=True)
if not tests:
    raise SystemExit('no audio generated; manifest not written')
if target.exists() and json.loads(target.read_text(encoding='utf-8')).get('tests'):
    raise SystemExit('smoke manifest exists; refusing overwrite')
target.write_text(json.dumps({'schema_version':1, 'tests':tests}, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'synthetic_fixture_count={len(tests)}')
