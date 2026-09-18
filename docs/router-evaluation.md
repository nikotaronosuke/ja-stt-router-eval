# Router 評価

短い日本語の発話を、限られた候補 ID のいずれか、または「該当なし」へ振り分ける router を、
provider を変えても同じ条件・同じ正解データで比較する。

## 評価セット

`fixtures/router-eval-v1/`（dataset 名 `ja-router-eval-v1`）。架空の市民センターの案内デスクを題材に、
来訪者の発話 242 件を 16 件の案内項目へ振り分ける。構成と付け方は
[fixtures/router-eval-v1/README.md](../fixtures/router-eval-v1/README.md) を参照。

| カテゴリ | 件数 | 狙い |
| --- | ---: | --- |
| direct | 40 | 関連語がそのまま出る。ちょうど 1 件の候補の関連語だけを含む |
| paraphrase | 40 | 関連語を使わない言い換え |
| indirect | 30 | 遠回しな尋ね方 |
| follow_up | 22 | 直前の発話が必要な追加質問 |
| multiple | 20 | 2 件以上が妥当 |
| none | 25 | 挨拶・雑談・進行。関連語を含まない |
| ambiguous | 18 | 解釈が複数あり、選ばない方がよい |
| distractor | 20 | 関連語を含むが案内を求めていない |
| scope_trap | 15 | 候補の語を含むが、候補の範囲外を尋ねる |
| very_short | 12 | 「いつから？」等。10 件は直前の発話つき、2 件は文脈なしで abstain が正解 |

封印日: 2026-09-19（`seal.json`）。封印後に発話・ラベルを変更した場合は新しい dataset として扱い、
それ以前の結果と比較しない。

## 実行手順

```
python scripts/validate_router_fixture.py              # 構造と keyword baseline の形を検査
python scripts/run_router_eval.py --by-category         # keyword baseline（ローカル、通信なし）
python scripts/run_router_eval.py --provider openai --dry-run
python scripts/run_router_eval.py --provider openai --confirm-send --budget-usd 0.50
```

hosted provider の run は、precheck 通過・見積もりが予算内・API キーあり・`--confirm-send` の
4 条件が揃ったときだけ送信する。`--model` と価格は設定であり、実行前に provider の公式情報で
確認する。結果は `artifacts/router-eval/` に書き、Git 管理外に置く。

### 送信されるもの

現在の発話、直前の発話（あれば）、各候補の id・title・triggers、固定の指示文と JSON schema。
候補の description、expected / acceptable のラベル、notes は送らない
（`tests/router/test_openai_router.py` と `router_eval/precheck.py` が検査する）。

## keyword baseline（封印済み v1 での実行結果）

`router_eval/keyword.py` の決定方針は「発話に関連語を含む候補がちょうど 1 件なら選ぶ。0 件と 2 件以上は
abstain」。決定的で通信を伴わないため、この結果は fixture と実装が同一なら誰が実行しても同じになる。

```
provider=keyword  score_type=trigger_match_count  dataset=ja-router-eval-v1  n=242
  expected_hit       42   17.4%
  acceptable_hit      7    2.9%
  wrong_display      27   11.2%
  correct_abstain    47   19.4%
  missed_but_safe   119   49.2%
  --- primary ---
  wrong_display_rate         0.1116
  correct_abstain_rate       0.5875
  missed_but_safe_rate       0.4917
  --- secondary ---
  expected_hit_rate          0.2593
  acceptable_or_better_rate  0.2025
  abstain_precision          0.2831
  abstain_recall             0.5875
  false_positive_rate        0.1116
  false_negative_rate        0.7407
  multiple_candidate_recall  0.975
```

| カテゴリ | n | expected | acceptable | wrong | abstain-ok | missed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| direct | 40 | 40 | 0 | 0 | 0 | 0 |
| paraphrase | 40 | 0 | 0 | 0 | 0 | 40 |
| indirect | 30 | 0 | 0 | 0 | 0 | 30 |
| follow_up | 22 | 1 | 1 | 0 | 0 | 20 |
| multiple | 20 | 1 | 0 | 0 | 0 | 19 |
| none | 25 | 0 | 0 | 0 | 25 | 0 |
| ambiguous | 18 | 0 | 0 | 0 | 18 | 0 |
| distractor | 20 | 0 | 0 | 19 | 1 | 0 |
| scope_trap | 15 | 0 | 6 | 8 | 1 | 0 |
| very_short | 12 | 0 | 0 | 0 | 2 | 10 |

読み方:

- direct 40/40、none 25/25 は fixture の設計どおり（`scripts/validate_router_fixture.py` が保証する）。
- paraphrase と indirect は関連語を使わないため、文字列一致では全件 `missed_but_safe` になる。
  これは安全側の失敗で、`wrong_display` には数えない。
- distractor と scope_trap の `wrong_display` が、文字列一致 router の主な害。関連語を含むが
  案内を求めていない発話に候補を出してしまう。
- `multiple_candidate_recall` が高いのは、一致した候補をすべて alternatives に載せるため。
  selected は 2 件以上の一致で abstain するので、multiple の大半は `missed_but_safe`。

## hosted provider の結果について

このリポジトリには hosted provider の実測結果を含めない。provider ごとの結果は Git 管理外の
`artifacts/router-eval/` に出力し、評価セットとランナーだけを共有資産として扱う。

この評価セットは 2026-09-19 に新規作成・封印したもので、それ以前に別の fixture・別の指示文で行った
測定とは比較できない。過去の数値をこの fixture の成績として掲載しない。
