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
- build入力は公式`https://github.com/ManabiGrid/manabigrid`をoriginに持つcleanなGit checkoutだけを許可する。dirtyな正本、fork、コピーへ差し替えて公式コミット由来のように表示しない。
- SHAを省略したローカルbuildでも、正本HEADがfetch済み`origin/main`と完全一致しなければ停止する。cleanでも未pushの正本commitは公式スナップショットとして扱わない。固定再現時は`--expected-source-sha`で対象SHAを明示する。

## 通常更新の唯一の入口

まず読み取りだけで現在地を確認する。

```bash
python3 update_pages.py status
```

出力は`published_state`（公開レポートを検証できたか）、`source_sync`（正本と公開版の一致）、`site_sync`（公開版がどのsite commitで生成されたか）、`release_readiness`（site checkoutを安全に公開へ使えるか）、`operational_readiness`（日次workflowがactiveで最近動いたか）の五軸で判定する。公開レポートの取得失敗・不正JSON・SHA欠落は`unknown`とし、正本更新やsite releaseを推測しない。checkout側に他のblock理由がなければ`blocked_published_state_unknown`で停止する。site checkoutの`origin`は`ManabiGrid/manabigrid-site`と完全一致させ、GitHub CLIは`--repo ManabiGrid/manabigrid-site`と`GH_HOST=github.com`を固定し、外部環境の`GH_REPO`を除外して、別repo・別GitHub hostへ誤送信しない。トップレベル`status`だけを見ても、dirty、branch違い、site drift、workflow契約違い、scheduled workflowの無効化・停滞を`current`と誤認しない。`publication_authority: not_observed`は、スクリプトが会話上の公開承認を推測しないことを示す。
`next_action_code`も破壊操作を指示しない。dirtyなら`preserve_and_inspect_dirty_worktree`、site driftなら`inspect_site_drift`として、reset・checkout・pull等を自動選択させない。更新可能でも公開承認を観測していないstatusでは`await_publication_approval`に留める。

明示承認があり、正本の現在`main`をすぐ公開する場合は次の1コマンドだけを使う。

```bash
python3 update_pages.py publish --approve-publication
```

承認時点のSHAが指定されている場合は固定する。正本`main`が1文字でも進んでいれば公開せず`blocked_source_drift`で止まる。

```bash
python3 update_pages.py publish --approve-publication --source-sha <40桁SHA>
```

外部URLへ実リクエストする一回検査は、依頼または承認がある大きな更新時だけ `--check-external-links` を足す。

## サイトコードだけを更新した時の公開確認

正本SHAがすでに公開版と同じでも、生成器・CSS・検査器のcommitを`main`へpushするとpush起点のPages workflowが再生成・公開する。未公開site commitを検出した通常更新の`publish`は`already_current`とせず、`blocked_site_release_requires_verification`で停止する。承認済みのsite commitをpushした後、次の入口でそのcommit専用run、公開`build-report.json`、Pages deploymentを完全一致で照合する。

```bash
python3 update_pages.py verify-site-release --site-sha <siteの40桁SHA> --source-sha <正本の40桁SHA>
```

この入口はcleanなsite `main`、local HEADと`origin/main`の一致、`Pages / push / <site SHA>`というrun-name全体、event、branch、workflow名、公開レポート内のsite／source両SHA、Pages deploymentの成功状態を検査する。別runや「最新run」を代用しない。Actionsはbuild時に`MANABIGRID_SITE_COMMIT_SHA`を与え、公開`build-report.json`の`publication.site_commit`へ生成器commitを記録する。

## runnerが保証すること

1. site checkoutがcleanな`main`で、local HEADと`origin/main`が同一か検査する。
2. 正本remote `main`の40桁SHAを取得し、その値をworkflowへ固定入力する。
3. 一意のUUIDを発行し、run-name全体、workflow、event、branch、site HEADが完全一致するrunだけを追跡する。「最新run」や部分一致から推測しない。
4. Actions内で正本remoteを再照合し、承認SHAからdriftしていればbuild前に停止する。
5. build、正本`materials/**/*.md`と`curriculum/PROGRESS_INDEX.md`の独立列挙との完全一致、全Markdown変換、内部リンク、SVG安全性、公開検疫、allowlist packageの全成功後だけPagesへdeployする。
6. deploy後に公開`build-report.json`の正本SHA、トップHTTP 200、不存在URLHTTP 404を照合する。
7. `--source-sha`省略時だけ、正本が公開中に進んだ場合は一度だけ最新SHAで追随する。明示SHAは固定し、公開後に進んでも新SHAを自動承認・再公開しない。
8. siteコードのpush更新では、該当push run、公開レポートのsite／source両SHA、Pages deploymentを`verify-site-release`で照合する。

公開workflowの最終結果が成功でも、runnerが`updated`、`already_current`、またはsiteコード更新時の`site_release_verified`を返すまでは完了と報告しない。

## 固定状態と対処

| 状態 | 意味 | 実行者の扱い |
|---|---|---|
| `current` / `already_current` | 公開SHAと正本SHAが一致 | 変更なしで完了 |
| `update_available` | 正本が公開SHAより進んでおり、release checkoutは利用可能 | 明示承認がある時だけpublishへ進む |
| `site_release_pending` / `blocked_site_release_requires_verification` | 正本SHAは同じだがsite commitが公開版と異なる | 正本更新扱いにせず、承認済みpush後にsite releaseを固定SHAで検証 |
| `updated` | 対象runと公開後照合まで成功 | 実測SHA・run URLを報告 |
| `site_release_verified` | site commit専用push run、公開両SHA、Pages deploymentが一致 | 実測site／source SHA・run URLを報告 |
| `dry_run_ready` | 公開直前のlocal契約まで成功 | 公開したとは報告しない |
| `blocked_missing_approval` | 公開承認なし | 実行しない |
| `blocked_source_drift` | 承認SHAと正本mainが不一致 | 新SHAを推測承認しない |
| `blocked_source_drift_after_publish` | 固定SHAの公開後に正本mainが進んだ | 公開済みSHAを記録し、新SHAを自動公開しない |
| `blocked_dirty_site` / `blocked_site_drift` | site checkoutがrelease状態でない | 差分を保持し、由来を確認する |
| `blocked_site_origin` | site checkoutのoriginが公式repositoryでない | remoteを自動変更せず、対象checkoutを確認する |
| `blocked_config_drift` | repository、base URL、正本URLがコード内の公式trust anchorと不一致 | 設定だけを信頼せず、変更意図を別レビューする |
| `blocked_published_state_unknown` | 公開レポートを取得・検証できず公開SHAが不明 | 更新あり／なしを推測せず、公開状態の取得原因を診断する |
| `blocked_contract_drift` | branchまたはworkflow契約が不一致 | 手作業で迂回しない |
| `blocked_schedule_disabled` / `blocked_schedule_stale` / `blocked_schedule_failed` / `blocked_schedule_in_progress` / `blocked_schedule_unverified_revision` | 日次更新が無効、72時間超未実行、直近が成功完了以外、または現在のsite HEADで未実行 | 正本が同じでも運用正常とは報告せず、workflow状態を診断する |
| `failed_workflow` | build・検査・deployのどこかが失敗 | runの最初の失敗ゲートを診断する |
| `failed_run_correlation` | 起動runをUUIDで一意に特定できない | 別runを成功扱いしない |
| `failed_live_verify` | Pagesと期待SHA／HTTP契約が不一致 | deploy成功だけで完了扱いしない |
| `blocked_missing_tool` / `failed_command` | `git`／`gh`等がない、認証・network・CLIが失敗 | 生の手動API操作へ迂回せず原因を報告する |

## 学習グリッドの生成契約

- 入口の固定slug、表示名、順序、`unit_id`接頭辞は`curriculum_grid.contract.json`を機械入力とし、生成器と独立検査器が中学・高校×5教科の固定期待値へ照合する。契約ファイルだけの誤編集で学校段階や教科を増減・改名できない。件数や状態をこの契約へ手書きしない。
- 計画範囲、単元名、学校段階・学年、工程状態は正本`curriculum/PROGRESS_INDEX.md`の「全単元一覧」から取得する。教材の掲載有無は正本`materials/`の実在パッケージから取得し、二つを混同しない。
- `準備中`は「進捗表に登録済みだが、このサイトで読める教材パッケージが0件」という表示状態であり、誰かが現在制作中、または完成予定があるという意味ではない。
- 新しい教材が同じ入口へ追加された時は、同じ`curriculum/<slug>/` URLのまま自動で「教材あり」へ昇格する。空のレッスンページ、将来の本文、完成日、目安時間を推測生成しない。
- 未知prefix、重複ID、未知状態、表header drift、罫線欠落、不正列、字下げされた表行、入口への0件／複数対応、PROGRESS_INDEXに対応しない教材パッケージはbuildを失敗させる。低effortの実行者が類似名や学年表記から補完しない。
- `check_site.py`は`build-report.json`の自己申告を信用せず、PROGRESS_INDEXとmaterialsを独立直読し、対象Markdownの集合・重複・出力先・変換件数、10入口、全単元、状態内訳、準備中の免責文、骨格ページの可視単元名・ID・学年群・折りたたみ状態要約・リンク・読み上げ名を照合する。

## 互換性修正が必要な場合

通常更新runnerはコードを自動修正しない。新しいMarkdown・SVG・正本構造でゲートが失敗した場合だけ、次の別レーンで扱う。

1. 失敗run、正本SHA、最初の失敗ファイルとエラーを固定する。
2. repo内のignored `review/`配下に新しい隔離出力を作り、同じSHAから再生成する。既存の`site-output/`や過去レポートを現行候補として流用せず、正本はread-onlyのままにする。
3. 安全性と意味を弱めない最小修正とnegative testを追加する。
4. `python3 -m unittest discover -s tests`、`python3 check_workflow.py`、`build_site.py --no-check`、`check_site.py`、`python3 device_matrix_check.py`、`package_site.py`を通す。
5. siteコードのcommit／push／Pages更新が明示承認されている場合だけ反映する。

repo内にignored `site-output/`が残っても、公開検査は`public_site.py`のallowlistだけを走査する。runner自身は生成物を作らず、Actionsの隔離checkoutでbuildする。

`check_workflow.py`は単なる文字列の存在ではなく、固定の日次cron、全jobのstep名・個数・順序、各step blockのSHA-256、deploy jobの`if`条件を構造位置ごとに照合する。コメントや`echo`、`if: false`、`continue-on-error`、検疫後の追加step、名前のないstep、別keyへ同じ文字列を書いてゲートを通すことはできない。workflowを意図的に変える時は、変更内容と負例をレビューしてから契約digestを更新する。

スマホ／タブレット互換性を変えるCSS・生成器修正では`device_matrix.contract.json`を入力に`python3 device_matrix_check.py`を実行する。固定10条件を削って不具合を消さず、追加が必要なら契約とnegative testを同時に更新する。文字200%条件は実機OS挙動の完全再現ではなく、reflow回帰を検出するCSS文字寸法proxyとして扱う。matrix reportは各profileの新規browser report、runner、ブラウザ検査器、CSS、生成器、契約のSHA-256と、文字倍率の適用前後実測値を持つ。古いreportの件数だけを現行コードの証拠に流用しない。

## 実行後に報告する最小証拠

- 正本SHA、公開`build-report.json`のSHA、site commit SHA。
- 対象Actions run URLと結論。
- Markdown変換件数、HTML件数、内部リンク切れ件数、公開検疫結果。
- トップ200／不存在404と、代表ページの実ブラウザ確認範囲。
- 残るblocked/failed状態。未実行の検証を「確認済み」と書かない。
