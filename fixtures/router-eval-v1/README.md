# 日本語 Router 評価セット v1（`ja-router-eval-v1`）

短い日本語の発話を、限られた候補IDのいずれか、または「該当なし」へ振り分ける router を、
provider を変えても同じ条件・同じ正解データで比較するための固定評価セットです。
特定のモデルに依存しない長期資産として扱います。

評価するのは「AIがそれらしい答えを返すか」ではありません。

- 正しい候補を選べるか
- 間違った候補を出さず abstain できるか
- 複数候補を適切に扱えるか

の3点です。

## 題材

架空の市民センターの案内デスクを題材にしています。来訪者の一言（質問・依頼・雑談）を、
16件の案内項目（会議室の予約、駐車場、Wi-Fi、落とし物、開館時間、備品の貸し出し…）の
どれか、または該当なしへ振り分けます。施設・人物・数値はすべてこの評価のために作ったもので、
実在の施設・組織・会話記録とは無関係です。

## 中身

| ファイル | 内容 |
| --- | --- |
| `candidates.json` | 架空の案内項目16件。`id` / `title` / `triggers`（関連語）/ `description`（表示専用） |
| `questions.json` | 架空の発話242件。10カテゴリ、正解ラベルと判断理由つき |
| `seal.json` | 上記2ファイルのSHA-256と件数。provider比較の前に封印した記録 |

実行するコードは `router_eval/evaluate.py`（ランナー）と `scripts/run_router_eval.py`（CLI）です。

router へ渡すのは `id` / `title` / `triggers` だけです。`description` は表示用の本文で、
router にも hosted provider にも渡りません（`router_eval/precheck.py` が送信前に検査します）。

## 候補16件

| id | 項目 | 意図的に近い相手 |
| --- | --- | --- |
| e01 | 会議室の予約方法 | e02 / e11 / e12 |
| e02 | 予約の変更と取り消し | e01 / e03 |
| e03 | 利用料金と支払い | e02 / e04 / e12 |
| e04 | 駐車場の利用 | e05 / e09 |
| e05 | 駐輪場の利用 | e04 |
| e06 | コピー機と印刷 | e03 |
| e07 | Wi-Fiの接続方法 | e09 |
| e08 | 落とし物と忘れ物 | — |
| e09 | 開館時間と休館日 | e04 / e07 |
| e10 | イベントと講座の申し込み | e11 |
| e11 | 団体登録 | e01 / e10 |
| e12 | 備品の貸し出し | e01 / e14 / e16 |
| e13 | ゴミの分別と持ち帰り | e15 |
| e14 | バリアフリー設備 | e12 |
| e15 | 館内での飲食 | e13 / e16 |
| e16 | 図書コーナーの利用 | e12 / e15 |

候補が完全に独立していると評価が簡単になりすぎるため、意味が部分的に重なる組を入れています。
例えば「借りたい」は備品（e12）の関連語ですが、車椅子（e14）や本（e16）の文脈でも自然に出ます。

## 発話242件

| カテゴリ | 件数 | 内容 |
| --- | --- | --- |
| direct | 40 | 関連語がそのまま発話に現れる。ちょうど1件の候補の関連語だけを含む |
| paraphrase | 40 | 関連語を使わず同じ意味を聞く |
| indirect | 30 | かなり遠回しに聞く |
| follow_up | 22 | 直前の発話を前提にした追加質問。`previous_question` が必要 |
| multiple | 20 | 2件以上の候補が妥当 |
| none | 25 | 挨拶・雑談・進行上の発言など。どの候補にも該当せず、関連語も含まない |
| ambiguous | 18 | 複数解釈があり、無理に選ばない方がよい |
| distractor | 20 | 候補の関連語を含むが、案内を求めていない（感想・状況説明・別の意味） |
| scope_trap | 15 | 候補の語を含むが、候補が扱う範囲の外（別の施設・提供していないサービス・事実と異なる前提）を尋ねる |
| very_short | 12 | 「いつから？」等。文脈なしでは判断困難 |

## 正解ラベルの付け方

各問は次を持ちます。

```
id / category / question / previous_question(任意)
expected_ids / acceptable_ids / should_abstain / notes
```

- **expected_ids** — 最も望ましい候補。`should_abstain` が真のときは必ず空。
- **acceptable_ids** — 完全な誤りではない代替。expected と重複させない。
- **should_abstain** — 何も出さないことが正解か。
- **notes** — なぜこのラベルなのかを日本語で1文。全問必須で、テストが空を弾きます。

### 作成の経緯（provenance）

この評価セットは、面接専用だった旧評価セットの構造（10 カテゴリとその件数、5 分類、
`expected_ids` / `acceptable_ids` / `should_abstain` / `notes` というラベル項目）を、
AI モデル（Claude、Claude Code 経由）が架空の市民センター案内デスクという別ドメインへ
移植して作成したものです。発話・候補・関連語・ラベル・notes はすべてモデルが生成し、
上のルール（曖昧すぎて正解を決められない発話は ambiguous へ移すか採用しない、
`should_abstain` と expected を矛盾させない、など）もその生成時にモデルが適用しました。
人が全 242 問を 1 問ずつ確認・承認する作業は行っていません。

機械的に検証した範囲は次のとおりです（`scripts/validate_router_fixture.py`、
`tests/router/test_dataset.py`、`tests/router/test_evaluate.py`）。

- id の一意性、カテゴリ名とカテゴリ別の件数、notes が空でないこと
- expected / acceptable の候補 id が存在し、互いに重複しないこと
- `should_abstain` が真なら expected が空、偽なら expected が 1 件以上あること
- follow_up / very_short は `previous_question` を持つか、abstain が正解であること
- multiple は expected が 2 件以上、none / distractor / scope_trap / ambiguous は全問 abstain が正解であること
- 16 候補すべてが expected または acceptable のどこかで参照されること
- keyword baseline の形：direct は expected 候補 1 件の関連語だけを含み全問 `expected_hit` になる、
  none は関連語を含まず全問 `correct_abstain` になる、distractor の少なくとも 1 問は関連語を含む
- hosted provider へ送る payload に候補の本文・ラベル・notes・禁止パターンが混入しないこと
  （`router_eval/precheck.py`）

各ラベルが意味的に正しいか（本当にその候補が該当するか、abstain が妥当か）は機械検証の対象外で、
人による全件レビューも行っていません。`questions.json` 冒頭の `note` にある「正解ラベルは人手で付け」
という文言は作成時のもので実態と異なります。封印済み fixture を書き換えると別の dataset になるため
ファイルはそのままにし、作成経緯についてはこの文書を正とします。

### should_abstain が真なのに acceptable がある場合

矛盾ではありません。「出さないのが安全だが、出しても誤りとまでは言えない」状態を表します。
主に scope_trap で使います。

- 発話が候補の範囲外を尋ねている（別の施設・提供していないサービス・事実と異なる前提）
  → `should_abstain: true`。
- そのうえで、近い候補が実際の条件を正直に示すことで部分的に答えになる場合は
  `acceptable_ids` に入れる。例: sco-003「駐車場は24時間出入りできますよね」に対する e04。
  駐車場案内は実際の利用時間を示すので、表示しても誤表示ではありません。
- 近い候補を出すと「そのサービスがある」と誤って示してしまう場合は `acceptable_ids` も空にする。
  例: sco-014「図書コーナーの本を買い取ってもらえますか」。

## 判定の5分類

ランナーは各問の結果を排他的な5つに分けます。

| 分類 | 意味 |
| --- | --- |
| `expected_hit` | expected の候補を選んだ |
| `acceptable_hit` | acceptable の候補を選んだ |
| `wrong_display` | どちらでもない候補を選んだ（**最重要の失敗**） |
| `correct_abstain` | 何も出さず、それが正解だった |
| `missed_but_safe` | 何も出さなかったが、本当は出すべきだった |

`wrong_display` より `missed_but_safe` を選びます。誤った候補を出すことは、何も出さないことより
悪いためです。そのため `wrong_display_rate` を accuracy に混ぜず、独立した主要指標として出します。

## スコアを確率として扱わない

provider のスコアは種類が違います（keyword は一致した関連語の数、hosted model は自己申告の
関連度）。ランナーはスコアを集計せず、`score_type` を記録するだけです。校正誤差や確率品質の
指標は意図的に持ちません。

## 封印（seal）

`seal.json` は `candidates.json` と `questions.json` の SHA-256 を記録します。`load_dataset` は
既定でこの封印を検証し、一致しない場合は読み込みを拒否します。評価結果を見てから発話や
ラベルを直す、という運用を仕組みで止めるためです。

fixture を直す必要があるときは、新しい dataset として扱います。

```
python scripts/validate_router_fixture.py --unsealed   # 直した内容を検査
python scripts/seal_router_fixture.py --reseal          # 封印し直す（sealed_on が更新される）
```

封印し直した後は、それ以前の provider の結果とは比較しません。

## 実行

```
python scripts/validate_router_fixture.py              # 構造と keyword baseline の形を検査
python scripts/run_router_eval.py --by-category         # keyword baseline
python scripts/run_router_eval.py --provider openai --dry-run
```

provider を増やすときは `scripts/run_router_eval.py` の `PROVIDERS` に1行足します。
評価セット側に provider 固有のコードは入れません。

## Git管理の範囲

- `candidates.json` / `questions.json` / `seal.json` / ランナー / この文書 → **Git管理**
- provider ごとの実測結果 → `artifacts/router-eval/` に出力し、**Git管理外**

hosted provider の規約で性能比較の公開に制限が生じる可能性があるため、測定結果はデータセットと
分けています。
