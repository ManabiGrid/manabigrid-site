---
title: main ruleset設定案とロールバック
audience: human
status: draft-not-applied
---

# main ruleset設定案とロールバック

## 結論

これは **設定案とローカルguarded runnerだけ** で、GitHubへはまだ適用していない。対象は
`ManabiGrid/manabigrid-site` の既定branch `main`。PRで全機械ゲートを
通したsiteコードだけをmainへ入れることが目的で、正本
`ManabiGrid/manabigrid`、日次Pages workflow、公開済みPagesは変更しない。

`apply_ruleset.py`は適用前snapshot、payload固定、作成、実効rules検証、
条件付きロールバックを1つの状態機械として実装する。通常の`preflight`は
read-onlyで、`apply`は新しい個別承認を得た時だけ使う。runnerを実装したことは
rulesetを適用したことも、将来の適用承認を得たことも意味しない。

2026-07-30のread-only API監査では、site repositoryのrulesetは0件だった。
比較対象の正本repositoryには、default branchを対象に削除・force push禁止、
PR、GitHub Actions status checkを要求するactive rulesetが1件あった。
この案はsite固有のcheck名へ置き換え、管理者の恒常bypassを置かない。

## 設定案

APIへ渡す候補は
`.github/rulesets/main.proposal.json`。主な効果は次のとおり。

- `main`の削除とforce pushを禁止する。
- direct pushではなくPRを必要とする。
- merge方式を通常のmerge commitだけに限定し、squash／rebaseをrulesetで拒否する。
  repository全体のmerge設定はこの案では変更しない。
- PR headがmainの最新状態を取り込んだうえで、GitHub Actionsの
  `manabigrid-site-pr-gate` が成功することを必須にする。
- 未解決のreview threadがある時はmergeを止める。
- 現在は単独maintainer運用を止めないため承認数を0とし、機械ゲートを必須に
  する。共同reviewerが常時参加できる状態になった場合だけ、別承認で1へ上げる。
- bypass actorは空。明示的なruleset bypass actor経路を置かない。

`manabigrid-site-pr-gate` はPR workflowのjob表示名である。workflowをmainへ導入し、
実PRでこのcheck名の成功を1回観測する前にrulesetを有効化してはならない。

## 適用前ゲート

次のすべてが揃った場合だけ、オーナーの新しい明示承認を受けて適用する。

1. `.github/workflows/pr-validate.yml` がmainに存在する。
2. 適用前7日以内、できれば同じ作業内の実PRで
   `manabigrid-site-pr-gate` が成功し、check名とGitHub Actionsのintegration IDを
   read-only APIで再確認した。古い成功runを流用しない。
3. 適用直前のruleset一覧・各detailと`rules/branches/main`の実効rulesを
   JSONで退避した。
4. PR workflowがdeploy action、Pages write、secretを持たないことを
   `check_pr_workflow.py`で再確認した。
5. その時点の公開Pages SHAと日次workflow状態を記録した。
6. repositoryのdefault branchが`main`であり、適用直前のruleset一覧が想定どおり
   0件である。同名または別rulesetが1件でもあればPOSTせず、stackする前に再設計する。
7. `active`作成の承認に、下記「条件付きロールバック」の対象ID、条件、期限も
   同時に含める。ロールバックが事前承認されない場合はactiveでPOSTしない。
8. 適用承認を、review済みsite commit SHAと
   `.github/rulesets/main.proposal.json`のSHA-256へ固定する。POST直前にlocal HEAD、
   公式remote `main`、clean worktree、payload SHA-256を再照合し、1つでもdriftしたら
   review済み入力とみなさず停止する。
9. `apply_ruleset.py`のpage flatten、正規化投影、state遷移、rollback preconditionと、
   malformed／trailing data／重複ID・rule／payload drift／同一IDの並行更新を
   拒否するnegative testが、別PRで独立review済みである。

この文書の存在、過去の公開承認、`gh`認証済み状態を適用承認の代わりにしない。

## guarded runner（ローカル実装・未実行）

runnerの固定入力は、公式repository、`main`、API version `2026-03-10`、
`.github/rulesets/main.proposal.json`である。任意のrepository、branch、payload path、
ruleset IDをCLIから差し込めない。`GH_REPO`、debug／TTY／pager環境変数を除去し、
shellを介さず、providerのraw bodyやstderrを利用者向け出力へ転送しない。

read-onlyの適用前確認は次の1コマンドで行う。cleanな`main`で、review済みsite SHAと
payload SHA-256を明示する。

```bash
python3 apply_ruleset.py preflight \
  --reviewed-site-sha "<40桁のreview済みsite SHA>" \
  --reviewed-payload-sha256 "<64桁のreview済みpayload SHA-256>"
```

`preflight`はlocal HEAD／remote main／origin／clean worktree／payload hash、
workflow checker、直近7日以内の実PR gateとGitHub Actions integration、
ruleset一覧0件、main実効rules 0件、Actions権限、PR／Pages workflow、
Pages environment、現行日次run、公開build-reportのsite／source SHAをread-onlyで
固定する。`--paginate --slurp`は外側と各pageを検査して1段だけflattenし、正常な
0件を`[[]]`に限定する。空応答、`[]`、途中page、非配列page、malformed、
trailing data、重複・bool・非正整数IDを0件へ読み替えない。

実適用は、オーナーの新しい個別承認が同じsite SHA、payload SHA-256、active作成、
15分以内の条件付き削除へ固定された時だけ次を使う。二つの技術flagは誤操作防止であり、
会話上の適用承認そのものではない。承認がなければ実行してはならない。

```bash
python3 apply_ruleset.py apply \
  --reviewed-site-sha "<40桁の承認済みsite SHA>" \
  --reviewed-payload-sha256 "<64桁の承認済みpayload SHA-256>" \
  --approve-ruleset-application \
  --approve-conditional-rollback
```

runnerはpayloadを1回だけUTF-8 bytesとして読み、duplicate key、BOM、NaN、
bool／int alias、schema、SHA-256を検証する。POST直前にfileが同じbytesか再確認し、
APIには検証済みbytesを`--input -`のstdinで渡す。hash確認後にpathを再入力として
開くTOCTOUを作らない。

POST intentはcredential、email、raw bodyを含めず、ignored
`review/ruleset-apply-transaction.json`へatomicに記録する。既存journalがあれば
上書きせず停止する。file本体に加えて親directoryも`fsync`し、POST前にintentの
永続化を確認できなければ作成しない。同期完了後のPOST直前に、payloadと適用前snapshot
をもう一度取得・照合し、壁時計とprocess内の単調時計で15分窓・非逆行を再確認する。
I/O中のremote drift、観測不能、期限超過が1つでもあればPOSTしない。
HTTP 201とstrictな単一JSON bodyから直接得た正整数IDだけを
`CREATED_ID_CONFIRMED`へ昇格する。create responseでoptionalな設定fieldが省略されても
ID確定と設定確定を混同せず、そのIDのdetailで完全照合する。timeout、切断、5xx、
status／exit code矛盾、malformed、trailing data、ID欠落では一覧差分から所有IDを
推定せず、同じPOSTを再送せず、`STATE_UNKNOWN_AFTER_POST`で停止する。

detailとmain実効rulesは、requestの6 fieldだけをallowlist投影する。top-levelの
既知server metadata（`id`、`node_id`、`source_type`、`source`、`created_at`、
`updated_at`、`current_user_can_bypass`、`_links`）と、main実効rule直下の既知metadata
（`ruleset_id`、`ruleset_source_type`、`ruleset_source`）だけを無視し、未知の
fieldはdefaultと推定せず意味的不一致にする。GitHubが返すoptional fieldのうち
`dismissal_restriction`は`enabled: false`かつ`allowed_actors: []`、
`required_reviewers`は空配列のexactなno-op値だけを許可して投影から除く。
enabled、actor、reviewerが1件でもあれば意味的不一致にする。rule typeはkey化して
重複・欠落・余分を拒否する。request定義fieldは型を含め完全一致させる。
main実効rulesは新ID由来の`deletion`、`non_fast_forward`、`pull_request`、
`required_status_checks`が1件ずつで、merge方式`merge`のみ、固定check名、
integration ID、bypassなしでなければならない。設定一致後の状態名は
`APPLIED_CONFIG_VERIFIED_BEHAVIOR_PENDING`であり、後続実PRのrule suiteを観測するまで
「阻止動作を実証済み」と報告しない。

## ロールバック手順案

条件付きロールバックも`apply_ruleset.py`だけが行う。生のDELETE commandを
copy-pasteしない。削除は外部の破壊操作なので、active適用と同時に事前承認された
同一transactionで、次の条件をすべて満たす場合だけ1回実行する。

1. POSTがHTTP 201で完了し、応答本文から正確な新規IDを直接取得できた。
2. POST直前にjournalへ永続化したローカルUTCのintent時刻から15分以内で、公式main
   SHAがPOST直前の記録と一致する。providerの`created_at`はidentity照合には使うが、
   rollback期限の時計には使わない。壁時計に加えて同一processの単調時計も使い、
   2回目の観測後とDELETE intent永続化後のDELETE直前にも両方を再取得する。
   期限超過、前回観測からの逆行、単調時計の異常があれば削除しない。
3. detailとmain実効rulesを完全取得でき、同じ安定した意味的不一致を独立した
   2回のread-only取得で再観測した。timeout、404、partial、parse不能、伝播待ちに
   見えるrule欠落は「不一致」に昇格せず、観測不能を削除理由にしない。
4. 適用前snapshotと現在値の差が、その新規IDだけである。
5. DELETE直前の新規ID detailが、`id`、`name`、`source_type: Repository`、
   `source: ManabiGrid/manabigrid-site`、作成直後に記録した`created_at`と`updated_at`、
   正規化応答のすべてで作成直後の記録と一致する。1項目でも取得不能なら
   「不変」と推定せず削除しない。
6. Actions権限、PR／Pages workflow、Pages environment、公開site／source SHA、
   現行日次runが適用前snapshotから不変である。DELETE intentをjournalへ同期した後にも
   detailとこのoperational snapshotを再取得し、2回目の不一致観測と完全一致しなければ
   DELETEしない。

1つでも満たさない、または事前承認がない場合は削除せず停止する。特に結果不明POSTの
曖昧なIDは条件付きロールバックの対象外である。事前承認なしに進める別案は、
`enforcement: disabled`で作成して検証後に別承認でactiveへ更新する方法だが、
このJSONはactive案のため、採用時はJSON・手順・testを別レビューする。

DELETEは作成応答から直接得たIDだけへ1回送る。HTTP 204＋空body後に、削除IDの
detail GETが404であること、ruleset一覧と
`rules/branches/main`が適用前snapshotへ戻ったこと、mainのSHAが変わっていないこと、
公開`build-report.json`のsite／source SHAが変わっていないこと、既存の日次workflowが
activeのままであることをread-onlyに照合する。rulesetの問題を直すためにPagesを
再deployしたり、正本を変更したりしない。

DELETEがtimeout、切断、malformed、予期しないstatusなら同じDELETEを再送しない。
read-only reconciliationでdetail 404と全baseline復元を証明できた場合だけ
`ROLLED_BACK_VERIFIED`へ昇格し、それ以外は`STATE_UNKNOWN_AFTER_DELETE`で停止する。
外部状態を検証できてもterminal journalを永続化できなければ成功exitにせず、
`JOURNAL_INCOMPLETE_AFTER_CREATE`または`JOURNAL_INCOMPLETE_AFTER_DELETE`として停止し、
`mutation_state`で作成済み・削除済みの実測を保持する。
GitHub DELETE APIには`If-Match`相当の原子的な条件付き削除がないため、最後のGETと
DELETEの間の競合を完全には排除できない。この残存リスクを許容できない適用では
条件付き自動削除を使わず、provider側の操作凍結と新しい個別承認へ切り替える。

## 戻し方の限界

- rulesetを戻しても、既にmainへmergeされたcommitは自動では消えない。
- workflow導入commit自体の取り消しが必要なら、別PRでrevertし、同じ機械ゲート
  を通す。`reset --hard`、force push、Pagesの手動差し替えは使わない。
- GitHubのAPI schema、integration ID、check名は時点依存である。適用時に公式
  APIとlive repositoryを再確認し、このdraftを無条件に実行しない。

## 防げる範囲

required status checkはcheck名とsource appを基準にし、workflow fileそのものを
暗号学的に固定する仕組みではない。GitHub公式仕様ではjob-levelの条件でskipされた
jobはSuccessとして報告され、required checkはsuccessful／skipped／neutralを
通過状態として扱う。一方、workflow全体がpath／branch filterでskipされた場合は
Pendingになり得るため、両者を混同しない。

現行PR workflowはpath filter、job／stepのskip、`continue-on-error`を禁止し、
固有job名、workflow全体digest、同名jobの重複検査、GitHub Actions app指定で
通常の改変を検出する。ただし、そのcheckerはrequired jobの中で動く。同じPRが
job自体をskipさせる、またはworkflow digestとcheckerを同時に書き換えると、
checkerが実行されないか自己改変を自己承認し得る。required check名とApp IDだけでは、
gate-coreの内容をrepository外から固定できない。

一方、repository管理権限を持つ攻撃者だけでなく、低effort実行者が同じPRで
workflow、checker、public allowlist、browser verifier、tests、MathML registryと
期待値を同時変更する誤更新も、同じrepository内のstatus checkだけでは独立判定
できない。さらにこの案の承認数は0なので、gate-coreの独立reviewは手続上の契約で
あり、provider側の強制ではない。この案はprovider側の部分的hardeningであって、
「non-bypassable」または完全な外部trust anchorとは表現しない。gate-core変更には
独立人間reviewを必要とする。独立reviewerを常時確保できる場合は承認数を1へ上げる。
organization-levelのruleset workflowやrepository Actions policyを使える
plan／現行仕様なら、base側で管理する追加trust anchorを別設定案として検討する。
Actions policyは2026-07-28時点でpublic previewのため、今回のdraftへ推測追加しない。

- GitHub公式: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- GitHub公式: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks
- GitHub公式: https://docs.github.com/en/rest/repos/rules?apiVersion=2026-03-10#create-a-repository-ruleset
- GitHub公式: https://docs.github.com/en/rest/about-the-rest-api/api-versions?apiVersion=2026-03-10
- GitHub公式: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/actions-policies/workflow-execution-protections
