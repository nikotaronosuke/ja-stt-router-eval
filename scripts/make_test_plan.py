"""Generate neutral public example sentences; never private material or historical inputs.

    python scripts/make_test_plan.py

Writes fixtures/test-plan.json once and refuses to overwrite it.
"""
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
questions = [
    ('今日の天気予報を確認してください。', ['天気予報'], []),
    ('図書館の開館時間を確認してください。', ['図書館'], []),
    ('明日の気温は何度でしょうか。', ['気温'], []),
    ('公園の入口は北側にあります。', ['公園', '入口'], []),
    ('この棚には青い箱が三つあります。', ['青い箱'], []),
    ('在庫確認は午後に行います。', ['在庫確認'], []),
    ('荷物は受付の横に置いてください。', ['荷物', '受付'], []),
    ('次の電車は十分後に到着します。', ['電車', '到着'], []),
    ('雨が降る前に窓を閉めてください。', ['雨', '窓'], []),
    ('明るさを少し下げてください。', ['明るさ'], []),
    ('音声の聞こえ方を確認します。', ['音声'], []),
    ('机の上に新しい地図があります。', ['地図'], []),
    ('赤いボタンを一度押してください。', ['赤いボタン'], []),
    ('緑のランプが点灯しています。', ['緑', 'ランプ'], []),
    ('時計を一時間進めてください。', ['時計'], []),
    ('箱の重さは二キログラムです。', ['重さ'], []),
    ('生成AIという語の発音を確認します。', [['生成AI', '生成エーアイ']], [['生成AI', '生成エーアイ']]),
    ('AIという略語を読み上げます。', [['AI', 'エーアイ']], [['AI', 'エーアイ']]),
    ('ウェブページの文字を大きくします。', ['ウェブページ'], []),
    ('画面の中央に丸を描きます。', ['画面', '中央'], []),
    ('WordPressという製品名を読み上げます。', [['WordPress', 'ワードプレス']], [['WordPress', 'ワードプレス']]),
    ('SharePointという製品名を読み上げます。', [['SharePoint', 'シェアポイント']], [['SharePoint', 'シェアポイント']]),
    ('信号が青に変わりました。', ['信号'], []),
    ('図書館では静かに話してください。', ['図書館'], []),
    ('会議室の予約を確認してください。', ['会議室', '予約'], []),
    ('本を二冊ずつ並べてください。', ['本'], []),
    ('水を半分まで注いでください。', ['水'], []),
    ('お湯の温度を測ってください。', ['温度'], []),
    ('風が少し強くなりました。', ['風'], []),
    ('地図の縮尺を確認してください。', ['縮尺'], []),
    ('鉛筆を三本用意してください。', ['鉛筆'], []),
    ('紙を半分に折ってください。', ['紙'], []),
    ('奥の扉をゆっくり開けてください。', ['扉'], []),
    ('帰る前に照明を消してください。', ['照明'], []),
    ('透明な袋に小物を入れます。', ['透明'], []),
    ('大きい箱を下に置いてください。', ['大きい箱'], []),
    ('合計金額は千円です。', ['合計金額'], []),
    ('進み具合は二十五パーセントです。', ['二十五パーセント'], []),
    ('最後の行をもう一度読みます。', ['最後'], []),
    ('これで音声テストを終了します。', ['音声テスト'], []),
]

tests = [{'test_id': f'q{i:02}', 'reference_text': text, 'keywords': keywords,
          'proper_nouns': nouns, 'decision_keywords': keywords,
          'condition': 'normal', 'audio_kind': 'human', 'audio': f'audio/q{i:02}.wav'}
         for i, (text, keywords, nouns) in enumerate(questions, 1)]
path = root / 'fixtures' / 'test-plan.json'
path.parent.mkdir(exist_ok=True)
if path.exists():
    raise SystemExit('test plan already exists; refusing overwrite')
path.write_text(json.dumps({'schema_version': 1, 'dataset_role': 'public_neutral_examples',
    'tests': tests}, ensure_ascii=False, indent=2), encoding='utf-8')
print('neutral_examples=40')
