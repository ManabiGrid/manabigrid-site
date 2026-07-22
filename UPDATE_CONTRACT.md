---
title: ManabiGrid展示サイト更新契約
audience: ai
status: active
---

# ManabiGrid展示サイト更新契約

この文書はCodex、Claude Code、その他の実行者に共通する正本です。モデル名やreasoning effortに依存せず、通常更新は同じ機械ゲートを通します。

## 変えてはいけない境界

- 教材の正本は `https://github.com/ManabiGrid/manabigrid` の `main`。サイト更新から正本を編集・commit・pushしない。
- 公開対象は `https://manabigrid.github.io/manabigrid-site/`。別ホスト、別repository、account、Pages設定、remote、依存関係を変更しない。
- 生成HTMLを手編集しない。正本の本文・数値を「サイト向け」に書き換えない。
- 検査失敗時にallowlist、検疫、期待件数を推測で緩めない。直前の正常なPagesは保持されるため、失敗したゲートと対象ファイルを報告して止める。
- `--approve-publication`は誤操作防止の技術フラグであり、承認そのものではない。現在のユーザー依頼がこのPages更新を明示承認している時だけ指定する。

## 通常更新の唯一の入口

まず読み取りだけで現在地を確認する。

```bash
python3 update_pages.py status
```

明示承認があり、正本の現在`main`をすぐ公開する場合は次の1コマンドだけを使う。

```bash
python3 update_pages.py publish --approve-publication
```

承認時点のSHAが指定されている場合は固定する。正本`main`が1文字でも進んでいれば公開せず`blocked_source_drift`で止まる。

```bash
python3 update_pages.py publish --approve-publication --source-sha <40桁SHA>
```

外部URLへ実リクエストする一回検査は、依頼または承認がある大きな更新時だけ `--check-external-links` を足す。

## runnerが保証すること

1. site checkoutがcleanな`main`で、local HEADと`origin/main`が同一か検査する。
2. 正本remote `main`の40桁SHAを取得し、その値をworkflowへ固定入力する。
3. 一意のUUIDを発行し、run-name全体、workflow、event、branch、site HEADが完全一致するrunだけを追跡する。「最新run」や部分一致から推測しない。
4. Actions内で正本remoteを再照合し、承認SHAからdriftしていればbuild前に停止する。
5. build、全Markdown変換、内部リンク、SVG安全性、公開検疫、allowlist packageの全成功後だけPagesへdeployする。
6. deploy後に公開`build-report.json`の正本SHA、トップHTTP 200、不存在URLHTTP 404を照合する。
7. `--source-sha`省略時だけ、正本が公開中に進んだ場合は一度だけ最新SHAで追随する。明示SHAは固定し、公開後に進んでも新SHAを自動承認・再公開しない。

公開workflowの最終結果が成功でも、runnerが`updated`または`already_current`を返すまでは完了と報告しない。

## 固定状態と対処

| 状態 | 意味 | 実行者の扱い |
|---|---|---|
| `current` / `already_current` | 公開SHAと正本SHAが一致 | 変更なしで完了 |
| `updated` | 対象runと公開後照合まで成功 | 実測SHA・run URLを報告 |
| `dry_run_ready` | 公開直前のlocal契約まで成功 | 公開したとは報告しない |
| `blocked_missing_approval` | 公開承認なし | 実行しない |
| `blocked_source_drift` | 承認SHAと正本mainが不一致 | 新SHAを推測承認しない |
| `blocked_source_drift_after_publish` | 固定SHAの公開後に正本mainが進んだ | 公開済みSHAを記録し、新SHAを自動公開しない |
| `blocked_dirty_site` / `blocked_site_drift` | site checkoutがrelease状態でない | 差分を保持し、由来を確認する |
| `blocked_contract_drift` | branchまたはworkflow契約が不一致 | 手作業で迂回しない |
| `failed_workflow` | build・検査・deployのどこかが失敗 | runの最初の失敗ゲートを診断する |
| `failed_run_correlation` | 起動runをUUIDで一意に特定できない | 別runを成功扱いしない |
| `failed_live_verify` | Pagesと期待SHA／HTTP契約が不一致 | deploy成功だけで完了扱いしない |
| `blocked_missing_tool` / `failed_command` | `git`／`gh`等がない、認証・network・CLIが失敗 | 生の手動API操作へ迂回せず原因を報告する |

## 互換性修正が必要な場合

通常更新runnerはコードを自動修正しない。新しいMarkdown・SVG・正本構造でゲートが失敗した場合だけ、次の別レーンで扱う。

1. 失敗run、正本SHA、最初の失敗ファイルとエラーを固定する。
2. OSの一時ディレクトリまたはignored `site-output/`へ同じSHAから再生成する。正本はread-onlyのままにする。
3. 安全性と意味を弱めない最小修正とnegative testを追加する。
4. `python3 -m unittest discover -s tests`、`python3 check_workflow.py`、`build_site.py --no-check`、`check_site.py`、`package_site.py`を通す。
5. siteコードのcommit／push／Pages更新が明示承認されている場合だけ反映する。

repo内にignored `site-output/`が残っても、公開検査は`public_site.py`のallowlistだけを走査する。runner自身は生成物を作らず、Actionsの隔離checkoutでbuildする。

## 実行後に報告する最小証拠

- 正本SHA、公開`build-report.json`のSHA、site commit SHA。
- 対象Actions run URLと結論。
- Markdown変換件数、HTML件数、内部リンク切れ件数、公開検疫結果。
- トップ200／不存在404と、代表ページの実ブラウザ確認範囲。
- 残るblocked/failed状態。未実行の検証を「確認済み」と書かない。
