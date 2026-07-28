---
title: main ruleset設定案とロールバック
audience: human
status: draft-not-applied
---

# main ruleset設定案とロールバック

## 結論

これは **設定案だけ** で、GitHubへはまだ適用していない。対象は
`ManabiGrid/manabigrid-site` の既定branch `main`。PRで全機械ゲートを
通したsiteコードだけをmainへ入れることが目的で、正本
`ManabiGrid/manabigrid`、日次Pages workflow、公開済みPagesは変更しない。

2026-07-28のread-only API確認では、site repositoryのrulesetは0件だった。
比較対象の正本repositoryには、default branchを対象に削除・force push禁止、
PR、GitHub Actions status checkを要求するactive rulesetが1件あった。
この案はsite固有のcheck名へ置き換え、管理者の恒常bypassを置かない。

## 設定案

APIへ渡す候補は
`.github/rulesets/main.proposal.json`。主な効果は次のとおり。

- `main`の削除とforce pushを禁止する。
- direct pushではなくPRを必要とする。
- PR headがmainの最新状態を取り込んだうえで、GitHub Actionsの
  `manabigrid-site-pr-gate` が成功することを必須にする。
- 未解決のreview threadがある時はmergeを止める。
- 現在は単独maintainer運用を止めないため承認数を0とし、機械ゲートを必須に
  する。共同reviewerが常時参加できる状態になった場合だけ、別承認で1へ上げる。
- bypass actorは空。モデルや実行者が「緊急」を推測してcheckを飛ばせない。

`manabigrid-site-pr-gate` はPR workflowのjob表示名である。workflowをmainへ導入し、
実PRでこのcheck名の成功を1回観測する前にrulesetを有効化してはならない。

## 適用前ゲート

次のすべてが揃った場合だけ、オーナーの新しい明示承認を受けて適用する。

1. `.github/workflows/pr-validate.yml` がmainに存在する。
2. 適用前7日以内、できれば同じ作業内の実PRで
   `manabigrid-site-pr-gate` が成功し、check名とGitHub Actionsのintegration IDを
   read-only APIで再確認した。古い成功runを流用しない。
3. 適用直前のruleset一覧と詳細をJSONで退避した。
4. PR workflowがdeploy action、Pages write、secretを持たないことを
   `check_pr_workflow.py`で再確認した。
5. その時点の公開Pages SHAと日次workflow状態を記録した。
6. repositoryのdefault branchが`main`であり、適用直前のruleset一覧が想定どおり
   0件である。同名または別rulesetが1件でもあればPOSTせず、stackする前に再設計する。

この文書の存在、過去の公開承認、`gh`認証済み状態を適用承認の代わりにしない。

## 適用手順案（未実行）

以下は将来の承認済み作業で使う候補であり、今回は実行しない。

```bash
gh api --hostname github.com \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  repos/ManabiGrid/manabigrid-site
gh api --hostname github.com \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  repos/ManabiGrid/manabigrid-site/rulesets
gh api --hostname github.com \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  --method POST \
  repos/ManabiGrid/manabigrid-site/rulesets \
  --input .github/rulesets/main.proposal.json
```

最初の2コマンドの応答から、default branchが`main`でない、ruleset一覧が0件でない、
同名rulesetがある、または一覧を完全取得できない場合は、3番目のPOSTを実行しない。
作成応答のruleset ID、作成前一覧、作成後詳細、対象ref、required check名を
同じ検証記録へ残す。応答が案と異なる、またはcheck名を解決できない場合は
mainへ変更を入れずロールバックへ進む。

POSTがtimeout・切断等で結果不明になった場合は、同じPOSTを再送しない。適用前の
ruleset ID集合とfreshな一覧・各detailを照合し、新規IDを一意に確定できた時だけ
作成済みとして扱う。確定不能なら追加作成も削除もせず停止する。新規IDを確定後に
削除する場合も、下記ロールバック用の新しい明示承認を得る。

## ロールバック手順案

今回は新規作成案なので、適用直後のロールバックは「作成応答で得た正確な
ruleset IDを指定して削除」が最小である。IDを名前や一覧順から推測しない。
削除は外部の破壊操作なので、その時点でも明示承認が必要。

```bash
gh api --hostname github.com \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  --method DELETE \
  repos/ManabiGrid/manabigrid-site/rulesets/<作成応答のruleset-id>
gh api --hostname github.com \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  repos/ManabiGrid/manabigrid-site/rulesets
```

削除後は、一覧が適用前snapshotと一致すること、mainのSHAが変わっていないこと、
公開`build-report.json`のsite／source SHAが変わっていないこと、既存の日次
workflowがactiveのままであることをread-onlyに照合する。rulesetの問題を直す
ためにPagesを再deployしたり、正本を変更したりしない。

## 戻し方の限界

- rulesetを戻しても、既にmainへmergeされたcommitは自動では消えない。
- workflow導入commit自体の取り消しが必要なら、別PRでrevertし、同じ機械ゲート
  を通す。`reset --hard`、force push、Pagesの手動差し替えは使わない。
- GitHubのAPI schema、integration ID、check名は時点依存である。適用時に公式
  APIとlive repositoryを再確認し、このdraftを無条件に実行しない。

## 防げる範囲

required status checkはcheck名とsource appを基準にし、workflow fileそのものを
暗号学的に固定する仕組みではない。この案は固有job名、workflow全体digest、同名
jobの重複検査、GitHub Actions app指定により、gate-coreがreview済みの時の
別モデルや低effort実行者による誤った省略・`echo`・skip・同名check追加を停止する。

一方、repository管理権限を持つ攻撃者だけでなく、低effort実行者が同じPRで
workflow、checker、public allowlist、browser verifier、tests、MathML registryと
期待値を同時変更する誤更新も、同じrepository内のstatus checkだけでは独立判定
できない。gate-core変更には独立人間reviewを必要とする。独立reviewerを常時確保
できる場合は承認数を1へ上げる。organization-levelのruleset workflowやrepository
Actions policyを使えるplan／現行仕様なら、base側で管理する追加trust anchorを
別設定案として検討する。Actions policyは2026-07-28時点でpublic previewのため、
今回のdraftへ推測追加しない。

- GitHub公式: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- GitHub公式: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/actions-policies/workflow-execution-protections
