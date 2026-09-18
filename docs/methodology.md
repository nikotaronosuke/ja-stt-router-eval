# 評価方法

このリポジトリの数値が何を意味し、何を意味しないかを定める文書。指標の定義は実装
（`stt_eval/metrics.py`、`router_eval/evaluate.py`）と一致させ、食い違えば実装を正とする。

## 共通の原則

- **分母を必ず書く。** 到達率・回収率・成功率は「件数／母数」を併記する。
- **欠測を 0 にしない。** partial が来なかった、final が来なかった、注釈がない、参照が未確認、
  のいずれも `None` として数え、分位点は取得できた件数と全件数を両方示す。
- **初回だけを品質に使う。** 長時間試験で同じ音源を繰り返した結果は安定性の証拠であり、
  精度の独立標本として水増ししない。
- **人工条件を自然条件と呼ばない。** 減衰・白色雑音・合成音声は、それぞれの名前で集計する。
  実話者の小声・早口・言い淀み・会議 codec の代わりにはならない。
- **historical measurement。** 音源・manifest・生ログはこのリポジトリに含めない。
  `docs/stt-evaluation.md` の数値はすべて過去の環境で取得したもので、このリポジトリだけでは
  再現できない。条件が異なる測定を同じ表に混ぜない。
- **自動判定できないものは未判定のまま残す。** 意味を壊す誤認識は CER から推定せず、
  人がテキストを照合した件数だけを別表で示す。

## STT 評価

### 公平性の手順（`stt_eval/harness.py`）

1. テスト用 WAV を一度読み、正規化した 16 kHz mono PCM16 を「共通原本」にする。
   原本の SHA-256 と変換条件を結果に残す。
2. 共通原本をローカルエンジンへは直接、hosted エンジンへは状態を保持する因果 FIR で 24 kHz に
   変換して渡す（`stt_eval/audio.py`）。
3. 20 ms チャンクを実時間でペーシングし、共通の Windows monotonic T0 から測る。
   各エンジンのイベント受信時刻も Windows 側で記録し、WSL の時計を引き算しない。
4. 各発話に 1 秒の末尾無音を付け、final を待ってから次の発話へ進む。同時実行（負荷は共有）と
   エンジン単独実行（負荷比較用）を区別して記録する。
5. モデルは開始前に warm-up し、ロード時間と初期メモリは STT 遅延と別に記録する。
6. 参照文と重要語は host 側だけが持ち、worker には音声しか渡さない。

### 指標の定義（`stt_eval/metrics.py`）

- **CER**: Unicode NFKC、casefold、空白と句読点の除去後の文字 Levenshtein 距離 ÷ 参照文字数。
  漢数字・アラビア数字・カナ表記の意味的な正規化はしない。micro（全参照文字で重み付け）と
  macro（音声ごとの平均）を分けて示す。
- **Keyword recall / proper-noun accuracy**: 事前に固定した語（許容別名を含む）が final に
  含まれる数 ÷ 対象語数。結果を見てから別名を追加しない。
- **First partial**: 文字または数字を含む最初の非 final イベント。final しかなければ `None`。
- **Keyword arrival**: `decision_keywords` がすべて揃った最初のイベント。発話開始からの時間なので、
  語が話されるまでの時間を含む。
- **Keyword end latency**（`stt_eval/keyword_timing.py`）: 人が聞いて確認した語の言い終わり位置から、
  その語を含む最初のイベントまで。予測が先行すれば負値を残す。人が確認していない注釈は使わない。
- **Stable**: final と同じ正規化文字列が最後まで維持され始めた最初のイベント（事後計算）。
- **修正回数**: 前の文字列への単純追記ではなく、既存部分の書き換えが起きた回数。
- **分位点**: nearest-rank の P50 / P90 / P95 / max。取得件数と全件数を併記する。

### 負荷の見方

- ホストプロセス CPU は全論理 CPU で正規化した値、WSL worker CPU は 1 コア = 100% の値。単純合算しない。
- GPU 使用率と VRAM は GPU 全体の値で、他のアプリの分を含む。モデル固有の CUDA allocated / reserved は別記録。
- Windows の物理空きメモリとコミット余裕は PDH の英語カウンターで読む。Pages Input にはマップ済み
  ファイル由来も含まれるため、それだけでスワップと断定しない。
- 「終盤 60 点 − 序盤 60 点の中央値差」はリークの有無を断定する検査ではない。

### 自然発話と会議経路

- 録音は明示的な操作でだけ始まり、最大 30 秒で止まる（`stt_eval/recording_study.py`）。
- 参照文は録音を聞いた人が確定する。認識結果の下書きを直した参照は `human_review_of_asr_draft`
  と記録し、独立した白紙の書き起こしとは区別する。
- 語の言い終わり位置は、その位置で止まる再生を人が聞いて確認したものだけを注釈にする。
  自動 timestamp は移動の補助に限る。
- 会議経路は受信 PC の WASAPI 取得以降を測る。遠端マイクとネットワークの遅延は含まない。
- 指定語が参照に存在しない録音は回収率の分母から除く（ガイドは語が話された証拠ではない）。

## Router 評価

### 契約（`router_eval/contract.py`）

- Router が受け取るのは `RouterInput`（question / previous_question / candidates）だけ。候補は
  `CandidateView`（id / title / triggers）で、表示用の本文は渡さない。
- Router が返すのは `RouterResult`（provider / score_type / selected_id / alternatives / abstain /
  latency_ms / metadata）だけ。人が読む文章を入れるフィールドはなく、metadata は短いスカラーに限る。
- `abstain` と `selected_id` の不一致は構築時に拒否する。

### 5 分類と指標（`router_eval/evaluate.py`）

| 分類 | 意味 |
| --- | --- |
| `expected_hit` | expected の候補を選んだ |
| `acceptable_hit` | acceptable の候補を選んだ |
| `wrong_display` | どちらでもない候補を選んだ |
| `correct_abstain` | 何も出さず、それが正解だった |
| `missed_but_safe` | 何も出さなかったが、出すべきだった |

主要指標は `wrong_display_rate`（全問中）、`correct_abstain_rate`（should_abstain 中）、
`missed_but_safe_rate`（全問中）。副次指標に `expected_hit_rate`（expected を持つ問中）、
`acceptable_or_better_rate`、`abstain_precision`、`abstain_recall`、`false_positive_rate`、
`false_negative_rate`、`multiple_candidate_recall`（multiple カテゴリで expected のうち selected と
alternatives に現れた割合）を出す。

### スコアの扱い

`score_type` は結果ごとに必ず記録し、同じ種別の間でしか比較しない。keyword の一致数と hosted model の
自己申告関連度を同じ確率として扱わないため、校正誤差や AUC のような指標は意図的に持たない。

### 封印と送信前検査

- fixture は provider 比較の前に `scripts/seal_router_fixture.py` で封印する。`load_dataset` は封印を
  検証し、変更後の fixture を黙って読まない。評価後に発話やラベルを直したら、それは新しい dataset。
- hosted provider には `router_eval/precheck.py` を通してからしか送らない。送る payload を実際に組み立て、
  候補の本文・ラベル・notes が含まれないこと、メールアドレス・ローカルパス・URL・鍵らしい文字列・
  実在企業の接尾辞がないこと、`store: false` であることを確かめる。
- 費用の見積もりは tokenizer なしの上限値で、実測 usage は API の応答から読む。予算超過が見えたら
  送らない。途中で予算に達した run は結果として報告しない。

## 個人情報と公開範囲

- fixture は完全に架空で、実在の施設・人物・組織・会話記録を含まない。
- 音声・manifest・録音・認識全文・生ログ・モデル・仮想環境・API キー・provider ごとの結果は
  Git 管理外（`.gitignore`）で、`artifacts/` に残る。
- 公開前に `scripts/privacy_scan.py` を通す。秘密鍵・token・メール・個人フォルダー・UUID・署名付き URL・
  私設 IP・電話番号・企業接尾辞・バイナリ形式を検査し、必要なら Git 管理外の追加語リストで固有の語を足す。
