# 評価設計の判断

[English](design-decisions.en.md) | 日本語

この文書は2026-09-20に、既存の公開コミット、評価methodology、sealed fixtureの文書をもとに後から整理したものです。同時進行で書かれた判断ログではありません。各項目は、リンク先の公開資料で確認できる内容だけに絞っています。

## 1. 本体へ組み込む前に評価ハーネスを独立させた

STTやrouterを先に製品へ入れると、UI、音声経路、prompt、network、hardware負荷など複数の要因が混ざります。

そこで採用前に評価repoを切り出し、共通の入力・metricsで比較できる土台を先に作りました。

**Evidence:** [initial harness](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/d0b81f0fe35ba9dc0591f37e1a31eab96132a5a6)

## 2. wrong display を safe miss より重く扱った

Routerを1つのaccuracyだけで評価すると、「何も出さない」と「間違った候補を出す」が同じ1ミスとして扱われます。

対象UIでは誤った候補を表示する方をより重い失敗とし、

- `expected_hit`
- `acceptable_hit`
- `wrong_display`
- `correct_abstain`
- `missed_but_safe`

の5分類に分け、`wrong_display_rate` を独立した主要指標にしました。

**Evidence:** [fixture README](../fixtures/router-eval-v1/README.md#判定の5分類)

## 3. provider結果を見る前にfixtureを封印した

評価結果を見たあとで発話やラベルを直すと、benchmarkをproviderの結果へ合わせられてしまいます。

そのため `candidates.json` と `questions.json` の SHA-256 を `seal.json` に記録し、既定のloaderは封印と一致しないfixtureを拒否します。内容を変えた場合は、新しいdatasetとして扱います。

**Evidence:** [fixture seal](../fixtures/router-eval-v1/README.md#封印seal)

## 4. AI生成fixtureを「人手ラベル」と読める説明を公開訂正した

初回公開後、fixture内の説明が「正解ラベルは人手で付けた」と読める一方、実際にはClaudeが架空の発話・候補・label・notesを生成し、人が242問すべてを1問ずつ確認・承認したわけではないことを確認しました。

sealed fixture自体を書き換えるとdatasetが変わるため、そのまま保持し、provenance文書側で実際の作成経緯を明記して訂正しました。

**Evidence:** [provenance correction](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/eef45514db141fa48020c7a9e27f35c239484124) / [fixture provenance](../fixtures/router-eval-v1/README.md#作成の経緯provenance)

## 5. 欠測を0にせず、再現できない過去測定もそう明記した

first partialが来なかった試行を0msにすると、「観測できなかった」が「瞬時に来た」という別の意味へ変わります。

そのためmissingはmissingのまま扱い、percentileにはobserved / totalの件数も併記します。

また、公開repoに元音声、manifest、生ログが含まれない過去STT数値は **historical measurement** とし、このrepoだけで同じ数値を再生成できるとは書いていません。

**Evidence:** [methodology](methodology.md) / [historical STT measurements](stt-evaluation.md)
