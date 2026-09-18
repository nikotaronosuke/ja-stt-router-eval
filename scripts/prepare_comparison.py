"""Freeze annotations and deterministic level/noise stress conditions before recognition.

    python scripts/prepare_comparison.py

Builds `fixtures/manifests/comparison-160.json` from the synthetic smoke manifest and
the FLEURS subset: 40 synthetic clips, 40 public read-speech clips, the same 40 clips
attenuated by 18 dB, and the same 40 clips with additive white noise at 10 dB SNR.
One content keyword per public utterance and selected proper nouns are fixed here,
before either model produces output, so no hint is derived from a result.
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stt_eval.audio import encode_pcm16, load_fixture  # noqa: E402

MANIFEST = ROOT / 'fixtures' / 'manifests' / 'comparison-160.json'
HUMAN = ROOT / 'fixtures' / 'manifests' / 'fleurs-ja-40.json'
SYNTHETIC = ROOT / 'fixtures' / 'manifests' / 'synthetic-smoke.json'
DERIVED_DIR = ROOT / 'fixtures' / 'audio' / 'derived'
NOISE_SEED_BASE = 20260916
# One content keyword per FLEURS utterance, in manifest order.
KEYWORDS = ['敵対的環境', '聖地', '公用語', '顎', '磁気反転', 'ヨット', '自己中心的', '音楽', '葬儀', '景色',
            '運転手', '貿易', '観光客', '家畜化', '警察本部', '微表情', 'マングローブ', '動詞', '恐竜', 'ビザ',
            '粉', '地殻', '温度', '出演者', '報道記者', '都市', 'キャンプ場', '洞窟', '観光', '危機管理室',
            '骨董品', '機能', '変革', '中枢神経系', '警告', '連敗', 'ズボン', 'カジノ', '病院', '羽毛']
# Proper nouns present in selected utterances (1-based index), with accepted spellings.
PROPER_NOUNS = {2: ['ファティマ'], 3: ['バルセロナ', 'カタルーニャ', 'スペイン'], 5: ['ロスビー'],
                9: ['サンピエトロ'], 10: ['香港', '九龍'], 12: ['モロッコ', 'カサブランカ'],
                14: ['イラン', 'ザグロス'], 15: ['トルコ', 'ガジアンテップ'],
                17: ['スンダルバンス', 'バングラデシュ', 'インド'], 20: ['シェンゲン'], 25: ['ダンディー大学'],
                26: ['アフリカ', 'アラビア'], 28: ['メッカ'], 29: ['香港'], 30: ['北マリアナ諸島'],
                32: [['ASUS', 'エイスース', 'アスース'], ['Eee PC', 'イーピーシー']], 33: [['USOC', 'ユーエスオーシー']],
                34: [['MS', 'エムエス']], 39: ['グレートヤーマス']}


def write_wav(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        with wave.open(stream, 'wb') as writer:
            writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            writer.writeframes(encode_pcm16(values))


def main():
    if MANIFEST.exists():
        raise SystemExit('Existing manifest preserved')
    human = json.loads(HUMAN.read_text(encoding='utf-8'))
    synthetic = json.loads(SYNTHETIC.read_text(encoding='utf-8'))
    tests = list(synthetic['tests'])
    for index, test in enumerate(human['tests'], 1):
        test = {**test, 'keywords': [KEYWORDS[index - 1]], 'decision_keywords': [KEYWORDS[index - 1]],
                'proper_nouns': PROPER_NOUNS.get(index, [])}
        tests.append(test)
        pcm, _ = load_fixture((MANIFEST.parent / test['audio']).resolve())
        values = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768
        rms = float(np.sqrt(np.mean(values ** 2)))
        for condition in ['attenuation_minus18db', 'white_noise_snr10db']:
            label = test['test_id'] + '_' + condition
            if condition == 'attenuation_minus18db':
                changed = values * 10 ** (-18 / 20)
                transformation = {'gain_db': -18,
                                  'interpretation': 'signal-level stress, not natural quiet/mumbled speech'}
            else:
                noise = np.random.default_rng(NOISE_SEED_BASE + index).standard_normal(len(values))
                noise = noise / np.sqrt(np.mean(noise ** 2)) * rms / 10 ** (10 / 20)
                changed = values + noise
                transformation = {'noise': 'Gaussian white', 'snr_db': 10, 'seed': NOISE_SEED_BASE + index,
                                  'interpretation': 'synthetic additive noise, not actual room noise'}
            clipped = float(np.mean(np.abs(changed) > 32767 / 32768))
            write_wav(DERIVED_DIR / f'{label}.wav', changed)
            tests.append({**test, 'test_id': label, 'condition': condition, 'audio': f'../audio/derived/{label}.wav',
                          'transformation': {**transformation, 'clipped_fraction': clipped}})
    MANIFEST.write_text(json.dumps({
        'schema_version': 1, 'provenance': human['provenance'],
        'annotation': 'One content keyword per human utterance and selected proper nouns; fixed before either '
                      'model output; no hints derived from references.',
        'tests': tests}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'tests': len(tests), 'human_original': 40, 'synthetic_examples': 40, 'human_derived': 80}))


if __name__ == '__main__':
    main()
