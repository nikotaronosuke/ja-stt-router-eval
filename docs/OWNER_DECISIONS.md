# Owner Decision Log

Japanese STT & LLM Router Evaluation は、モデルのランキングを作るためのリポジトリではありません。

本体へ組み込む前に、**日本語音声認識と「短い発話 → 候補ID」のルーティングを、
同じ条件・同じ評価ルールで比較できる状態にする**ために切り出した評価ハーネスです。

この文書では機能一覧ではなく、プロジェクトオーナーとして
**何を公平な比較とみなし、どの失敗を重く見て、どの数字を主張しないか**
が分かる判断だけをまとめます。

---

## 1. 本体へ先に組み込まず、評価ハーネスを独立させた

### 課題

日本語STTやrouterは、実際のアプリへ先に組み込んでも動作確認はできます。

しかしその状態では、

- UI処理
- 音声取得経路
- modelの違い
- prompt
- network
- hardware負荷

が混ざり、何が結果を変えたのか分かりにくくなります。

### 判断

まず評価そのものを独立したrepoへ切り出し、

- STT
- Router
- fixture
- metrics
- resource measurement

を本番アプリから分離しました。

「とりあえず動くものを作って体感で選ぶ」より、
**採用前に同じ物差しを作る**方を選びました。

**Evidence:** [README](../README.md) / [initial evaluation harness](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/d0b81f0fe35ba9dc0591f37e1a31eab96132a5a6)

---

## 2. STT比較で、エンジンごとに違う入力条件を使わなかった

### 課題

STTモデルを比較するとき、
各エンジンが扱いやすい形式へ個別に音源を調整すると、
モデル性能と前処理の差が混ざります。

### 判断

同じWAVを一度だけ正規化して**共通原本**を作り、
そのSHA-256と変換条件を結果へ残します。

- 16 kHz mono PCM16を共通基準
- hosted側に必要な変換は因果FIRで明示
- 20ms chunkを実時間ペーシング
- 同じWindows monotonic T0から計測
- WSL側の時計との差分をlatencyに使わない

という条件にしました。

モデルごとに都合のよい時計・入力へ寄せるのではなく、
**同じ開始点から比較する**判断です。

**Evidence:** [methodology — STT fairness](methodology.md#stt-評価)

---

## 3. 初回試行と反復試行を、同じ「精度データ」にしなかった

### 課題

同じ音源を何十回も流せば、サンプル数を簡単に増やせます。

しかし同じ発話の繰り返しは、
独立した日本語発話が増えたことにはなりません。

### 判断

**最初の試行を品質評価、反復を安定性評価**として分けました。

長時間試験で同じ音源を100回流しても、
それを「100件の精度サンプル」とは数えません。

測定回数を増やすことと、
評価対象の多様性を増やすことを分ける判断です。

**Evidence:** [methodology — common principles](methodology.md#共通の原則)

---

## 4. 欠測値を0msや0%に置き換えなかった

### 課題

partialが来なかった場合にlatency=0とすれば、
集計処理は簡単になります。

しかし「来なかった」と「即座に来た」は正反対です。

### 判断

取得できなかった値は **None / missing** として残します。

- first partialなし
- finalなし
- human annotationなし
- reference未確認

を0へ変換しません。

percentileを出すときも、

> 取得できた件数 / 全件数

を併記します。

きれいな表を作るために、
**欠測を性能値へ変換しない**判断です。

**Evidence:** [methodology — missing values](methodology.md#共通の原則)

---

## 5. 合成音声・ノイズ加工を「自然発話」と呼ばなかった

### 課題

合成音声や人工ノイズは再現性が高く、
benchmarkには便利です。

しかし、

- 人間の小声
- 早口
- 言い淀み
- 会議codec
- マイク特性

をそのまま代表するわけではありません。

### 判断

人工条件は、

- synthetic speech
- attenuation
- white noise
- degraded audio

としてそのまま記録し、
**自然発話の精度を証明したとは扱わない**ことにしました。

過去会話でも、同じ音源を直接STT、WASAPI経由、会議経路で比較し、
経路の差と認識器の差を分ける方針を先に置いていました。

**Evidence:** [methodology — artificial vs natural conditions](methodology.md#共通の原則)

---

## 6. Routerの仕事を「回答生成」ではなく、候補IDの選択だけにした

### 課題

Routerへカード本文や履歴を大量に渡し、
そのまま回答文まで作らせることもできます。

しかしそれでは、

> どの候補を選ぶ能力

と

> 文章を生成する能力

を同じ評価にしてしまいます。

### 判断

Routerのcontractは、

```text
question
previous_question
candidate id / title / triggers
       ↓
selected_id OR abstain
```

だけにしました。

表示用の長いdescriptionや正解labelは渡しません。

評価するのは「それらしい文章を返すか」ではなく、
**閉じた候補集合から安全に選択できるか**です。

**Evidence:** [methodology — Router contract](methodology.md#router-評価)

---

## 7. abstainを失敗ではなく、正式な正解として扱った

### 課題

Routerのaccuracyだけを見ると、
何も選ばないケースはすべて「不正解」になりがちです。

しかし実際のUIでは、
分からないときに間違ったカードを出すことの方が害になります。

### 判断

評価を5分類に分けました。

- expected_hit
- acceptable_hit
- wrong_display
- correct_abstain
- missed_but_safe

そして、

> **wrong_displayよりmissed_but_safeを選ぶ**

という価値判断を明示しました。

出すべきカードを出さない失敗と、
間違ったカードを自信ありげに出す失敗を同じ1ミスにしません。

**Evidence:** [fixture README — 5 classifications](../fixtures/router-eval-v1/README.md#判定の5分類)

---

## 8. accuracyに全部を畳み込まず、wrong_displayを独立した主要指標にした

### 課題

expected / acceptable / abstain / wrongを
1つのaccuracyへまとめれば比較は簡単です。

しかしaccuracyが同じでも、

- 何も出さないrouter
- 間違った候補を頻繁に出すrouter

では製品上の危険度が違います。

### 判断

`wrong_display_rate` を独立した主要指標にしました。

correct abstainやmissed but safeも別々に測ります。

**一つの高いaccuracyで安全性を隠さない**ための判断です。

**Evidence:** [router evaluation metrics](router-evaluation.md#keyword-baseline封印済み-v1-での実行結果)

---

## 9. providerが返すscoreを「確率」と呼ばなかった

### 課題

keyword baselineの「一致語数」と、
hosted modelの自己申告scoreを同じ0〜1へ正規化すれば、
1つのconfidence chartにできます。

しかし意味の違う数値です。

### 判断

各結果には `score_type` を必ず持たせ、
**同じscore type同士でしか比較しない**ことにしました。

provider scoreを校正済みprobabilityとはみなしません。

そのため、このrepoでは無理に

- calibration error
- AUC
- probability quality

を作りません。

数字の形が似ているからといって、
**同じ意味だと仮定しない**判断です。

**Evidence:** [methodology — score handling](methodology.md#スコアの扱い)

---

## 10. providerの結果を見てから評価fixtureを直せないようにした

### 課題

モデル評価後に、

> この問題は曖昧だったから削ろう  
> このlabelは違ったことにしよう

とfixtureを修正すると、
後からbenchmarkをモデルへ合わせられてしまいます。

### 判断

provider比較の前に、

- candidates.json
- questions.json

のSHA-256を `seal.json` に固定しました。

loaderはsealが一致しないfixtureを拒否します。

修正が必要なら**新しいdataset**として扱い、
過去のprovider結果とは比較しません。

レビュー手順ではなく、
**結果を見た後の無意識な調整を仕組みで防ぐ**判断です。

**Evidence:** [fixture README — seal](../fixtures/router-eval-v1/README.md#封印seal)

---

## 11. AI生成fixtureを「人手ラベル」と誤記していたことを隠さなかった

### 課題

公開後、router fixtureについて

> 正解ラベルは人手で付けた

と読める記述が、実態と違うことが分かりました。

実際には、

- 発話
- 候補
- trigger
- expected / acceptable
- abstain label
- notes

をClaudeが生成し、人が242問すべてを個別レビューしたわけではありませんでした。

### 判断

この事実をREADMEで明示的に訂正しました。

ただしsealed dataset本体を後から書き換えると別datasetになるため、
既存fixtureは変更せず、**provenance documentを正**としました。

「見栄えが悪くなるから黙る」でも、
「過去をきれいに書き換える」でもなく、
**何が機械検証され、何が人間確認されていないかを分けて公開**しています。

**Evidence:** [fixture provenance correction](https://github.com/nikotaronosuke/ja-stt-router-eval/commit/eef45514db141fa48020c7a9e27f35c239484124)

---

## 12. hosted providerへ送る前に、privacyと予算を機械的にgateした

### 課題

評価用fixtureでも、
将来の変更でローカルpath・メール・labelなどがpayloadへ混ざる可能性があります。

またbenchmarkを回すだけで想定外のAPIコストを使うのも避けたい問題です。

### 判断

hosted runは、

1. precheck通過
2. 予算内
3. API keyあり
4. 明示的な `--confirm-send`

の4条件が揃ったときだけ送ります。

precheckでは実際のpayloadを構築し、

- candidate description
- 正解label
- notes
- local path
- URL
- credentialらしい文字列

などが混ざっていないことを確認します。

budget到達途中のrunは評価結果として報告しません。

**Evidence:** [router evaluation — hosted run gates](router-evaluation.md#実行手順)

---

## 13. historical measurementを「このrepoで再現可能」とは言わなかった

### 課題

過去に取得したSTT実測値はあります。

しかし公開repoには、

- 音声
- manifest
- raw transcript
- raw log
- model checkpoint
- environmentそのもの

を含めていません。

### 判断

過去数値は明示的に **historical measurement** と呼び、

> このrepositoryだけでは再現できない

とREADMEとmethodologyへ書きました。

再現材料が足りないのに
「reproducible benchmark」と過大に主張しない判断です。

一方でRouter側のsealed fixtureとkeyword baselineは公開しており、
**再実行できる部分とできない部分を分けています。**

**Evidence:** [methodology — historical measurement](methodology.md#共通の原則) / [README](../README.md#what-is-not-in-this-repository)

---

## 14. provider別の実測結果を、評価資産そのものと分けた

### 課題

READMEにモデル別の勝敗表を載せると、
repoとしては分かりやすくなります。

しかし結果は、

- provider terms
- model version
- pricing
- prompt version
-実行日

に依存して変わります。

### 判断

Git管理する中心資産は、

- dataset
- methodology
- runner
- validation
- baseline

とし、providerごとの実測結果はGit管理外へ置きました。

評価ハーネスと、その時点の特定モデルの成績を同一資産にしない判断です。

**Evidence:** [router evaluation — hosted results](router-evaluation.md#hosted-provider-の結果について)

---

## このプロジェクトで優先したもの

Japanese STT & LLM Router Evaluationでは、
「一番強いモデルを決める」ことより次を優先しています。

- 本体実装より先に評価条件を固定する
- 同じ入力と同じ時計でSTTを比べる
- 反復試行を精度サンプルへ水増ししない
- 欠測を0にしない
- 人工条件を自然発話の証拠にしない
- Routerを回答生成から切り離す
- abstainを正常な結果として扱う
- wrong displayを安全なmissより重く見る
- 異なるscoreを同じprobability扱いしない
- provider結果を見る前にfixtureを封印する
- fixtureのprovenance誤記を隠さず訂正する
- hosted送信をprivacyと予算でgateする
- 再現できない過去測定を再現可能と呼ばない
- 評価方法と特定providerの勝敗を分ける

AIを使って実装・fixture作成をしていますが、
このリポジトリで重要なのはコード量ではなく、
**比較結果より先に「何を公平・安全な評価と呼ぶか」を決めたこと**です。
