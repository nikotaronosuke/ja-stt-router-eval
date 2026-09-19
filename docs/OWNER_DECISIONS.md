# Owner Decision Log

日本語 | [English](OWNER_DECISIONS.en.md)

このrepoで残すのは、評価の結果より「評価方法をどう守ったか」が分かる5件です。

## 1. 本体へ組み込む前に評価ハーネスを独立させた

STTやrouterを先に製品へ入れると、UI・音声経路・prompt・network・hardware負荷が混ざります。

そこで採用前に評価repoを切り出し、同じ入力・同じmetricsで比較する土台を先に作りました。

**Evidence:** [initial harness](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/d0b81f0fe35ba9dc0591f37e1a31eab96132a5a6)

## 2. wrong display を safe miss より重く扱った

Routerを1つのaccuracyだけで評価すると、「何も出さない」と「間違った候補を出す」が同じ1ミスになります。

実UIでは後者の方が危険なので、

- expected_hit
- acceptable_hit
- wrong_display
- correct_abstain
- missed_but_safe

の5分類に分け、`wrong_display_rate` を独立した主要指標にしました。

**Evidence:** [fixture README](../fixtures/router-eval-v1/README.md#判定の5分類)

## 3. provider結果を見る前にfixtureを封印した

評価後に「この問題は曖昧だった」とラベルや発話を直すと、benchmarkをモデルへ合わせられてしまいます。

比較前に `candidates.json` と `questions.json` の SHA-256 を `seal.json` へ固定し、変更されたfixtureはloaderが拒否するようにしました。

修正するなら別datasetとして扱います。

**Evidence:** [fixture seal](../fixtures/router-eval-v1/README.md#封印seal)

## 4. AI生成fixtureを「人手ラベル」と誤記していたことを公開訂正した

公開後、fixtureの説明が「正解ラベルは人手で付けた」と読める一方、実際には発話・候補・label・notesをClaudeが生成し、242問すべてを人が個別承認していなかったことを確認しました。

sealed fixture 自体を書き換えるとdatasetが変わるため、fixtureは保持し、provenance文書側を正として訂正しました。

**Evidence:** [provenance correction](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/eef45514db141fa48020c7a9e27f35c239484124)

## 5. 欠測を0にせず、再現できない過去測定もそう明記した

first partial が来なかった試行を 0ms にすると「来なかった」が「瞬時に来た」に変わります。

そのため missing は missing のまま数え、percentile には取得件数も併記します。

また公開repoに元音声・manifest・raw logがない過去STT数値は **historical measurement** とし、このrepoだけで再現できるとは書いていません。

**Evidence:** [methodology](methodology.md)
