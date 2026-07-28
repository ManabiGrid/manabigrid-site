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

`status`は成功時も例外時もJSONを標準出力へ返すだけである。repo内外のファイルを作成・更新せず、Git読取には`GIT_OPTIONAL_LOCKS=0`を強制してindex refreshやlock作成も抑止し、workflow dispatch、GitHub設定変更、commit、push、Pages更新を行わない。ignored `update-report.json`は状態snapshotではなく、公開後の完全照合を通った最後の`publication_verification`専用記録とする。`updated`、`site_release_verified`、公開自体は確認できた`blocked_source_drift_after_publish`、または公開確認後の正本HEAD再取得だけが失敗した`blocked_source_state_unknown_after_publish`だけを、正本SHA、site SHA、Actions runとともに一時ファイルから原子的に置換する。status、dry-run、already-current、公開前のblock／failure、一般例外はこの記録を上書きしない。

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
| `blocked_source_state_unknown_after_publish` | 対象run・公開両SHA・Pages・HTTPは完全照合済みだが、最後の正本main再取得だけが失敗 | 検証済み公開版を記録して非0停止し、正本の鮮度を推測しない |
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

## 閲覧UIの生成契約

- テーマは`system / light / dark`の3状態だけとする。既定は端末設定へ従う`system`で、明示選択だけを`manabigrid-theme`として同一originの`localStorage`へ保存する。検索語、読了、スクロール位置、教材選択を保存しない。
- `static/theme.js`だけが`localStorage`へ触れてよい。キー、許可値、読み書き箇所は`check_site.py`で完全一致検査し、別キーやネットワーク送信を追加しない。
- 図の拡大はページ内のネイティブ`dialog`を既定とし、URLと本文スクロール位置を変えない。閉じるボタン、背景、Esc、元リンクへのフォーカス復帰を維持し、JavaScript不成立時は元SVGを新規タブで開けるfallbackを残す。
- 図を拡大するために全ページのスクロール位置を永続保存しない。戻るべき対象が曖昧な一般ページ遷移へ状態を持ち越さない。
- ページ末の回復導線は「ページの先頭」と「ホームまたは教材一覧」の2つとし、本文を覆う固定ボタンにしない。44px以上の操作領域を実描画検査する。
- 横長SVGはviewBox幅500以上、または縦横比2以上を候補にする。ただしフォーカス可能領域、region名、操作ヒントは実描画で横幅が超過した時だけ付ける。表も同じ実幅基準を使う。
- 単元ページの番号付きレッスン行と、番号を持たない解答・案内・指導・制作資料の行を同じgrid列へ流し込まない。後者は本文幅1列で表示し、短い資料一覧が細い番号列へ折り返されて縦長になる回帰を契約テストで拒否する。
- 日本語本文はCSS禁則処理と利用可能な文節改行へ委ねる。意味や段落を変える自動`br`、句読点の移動、文字の補正は行わない。
- 分数は`n/d`という見た目だけで自動MathML化しない。比、単位、URL、日付、正誤記号等との区別を推測せず、インラインMathMLはsource path・元文字列・期待出現数・静的presentation MathML・読み上げ名を固定レジストリへ追加した対象だけに限定する。固定対象が欠落・増加した場合、未知の要素／属性が入った場合、3ページ以外へMathMLが現れた場合はビルドまたは公開検査を停止する。
- インラインMathMLは`semantics`と元文字列の`annotation encoding="text/plain"`を持ち、変数は`mi`、数は`mn`、演算子は`mo`、上下型分数は`mfrac`で表す。読み上げ用`aria-label`だけでなく、要素順・数値・符号・分子分母・属性を含む正規化MathML木を固定期待値へ完全一致させ、生成HTMLや正本を手編集して帳尻を合わせない。件数レポートは真偽値や小数で整数を代用しない。
- 全indexableページの`title`と`description`を重複不可にし、canonicalとsitemapの自己整合を検査する。検索エンジンへの登録、Search Console、URL検査依頼はGoogleアカウントを伴う外部操作として別承認レーンに置く。
- Search Console所有権確認値は`site.config.json`だけを正とし、トップ`index.html`の正規な`html > head`直下へ1件だけ生成する。他ページへの複製、生成HTMLの手編集、Analytics／Tag Manager／追跡コードへの置換を行わない。`check_site.py`は属性重複なし、値の完全一致、トップ1件、他ページ0件を公開ゲートとして検査する。Googleが所有権を定期的に再確認するため、確認後も設定値を削除しない。
- 公開ディレクトリ内のファイルは検査済み拡張子だけを許可し、`.htm`など意味的HTML検査の対象外になる形式をartifactへ含めない。新しい形式が必要な場合は、先に検査器とnegative testを追加してから許可集合を更新する。

## 互換性修正が必要な場合

通常更新runnerはコードを自動修正しない。新しいMarkdown・SVG・正本構造でゲートが失敗した場合だけ、次の別レーンで扱う。

1. 失敗run、正本SHA、最初の失敗ファイルとエラーを固定する。
2. repo内のignored `review/`配下に新しい隔離出力を作り、同じSHAから再生成する。既存の`site-output/`や過去レポートを現行候補として流用せず、正本はread-onlyのままにする。
3. 安全性と意味を弱めない最小修正とnegative testを追加する。
4. `python3 check_pr_workflow.py`、`python3 -m unittest discover -s tests -v`、`build_site.py --no-check`、`check_site.py`、`python3 package_site.py --dry-run`、`python3 device_matrix_check.py --site-root <fresh-output>`、`python3 negative_css_overflow_check.py --site-root <fresh-output>`を通す。
5. siteコードのcommit／push／Pages更新が明示承認されている場合だけ反映する。

repo内にignored `site-output/`が残っても、公開検査は`public_site.py`のallowlistだけを走査する。runner自身は生成物を作らず、Actionsの隔離checkoutでbuildする。

`check_workflow.py`は単なる文字列の存在ではなく、固定の日次cron、全jobのstep名・個数・順序、各step blockのSHA-256、deploy jobの`if`条件を構造位置ごとに照合する。コメントや`echo`、`if: false`、`continue-on-error`、検疫後の追加step、名前のないstep、別keyへ同じ文字列を書いてゲートを通すことはできない。workflowを意図的に変える時は、変更内容と負例をレビューしてから契約digestを更新する。

`.github/workflows/pr-validate.yml`はsiteコード専用の非deploy workflowである。`pull_request`からmainを対象とし、権限は`contents: read`だけ、job名は`manabigrid-site-pr-gate`へ固定する。正本mainの観測SHAをcheckoutし、全契約テスト、fresh build、独立check、公開検疫、固定11端末の実Chrome描画、意図的CSS横あふれを実描画gateが拒否する負例を同じ`site-output`へ順番に実行する。全gate後に正本mainを再取得し、冒頭の観測SHAから進んでいればgreenにせず、同じPR検証を再実行させる。Pages／ID token write、deploy/upload action、secret、path filter、`pull_request_target`、job／stepのskip、`continue-on-error`を追加しない。`check_pr_workflow.py`はworkflow全体のreview済みSHA-256に加えてtrigger、権限、action pin、step名・順序、必須command、検疫語彙、workflowファイル集合、他workflowによる必須check名のliteral／引用符付き／式生成shadow、正本SHAの冒頭・末尾2回照合を検査する。

CSS横あふれの検出器を変更した時は`python3 negative_css_overflow_check.py`で、一時的な公開候補だけへ意図的な`min-width`を加え、320px実描画が「ページ全体が横にはみ出しています」で非0停止することを確認する。未承認MathMLは、固定3ページ以外への追加と正規化tree／件数の改変を既存negative testが拒否する状態を維持する。失敗を消すために端末条件、対象ページ、MathML allowlist、検疫を緩めない。

main rulesetは`MAIN_RULESET_PROPOSAL.md`と`.github/rulesets/main.proposal.json`が未適用draftである。最初の実PRで必須check名とGitHub Actions integrationを観測し、適用直前snapshotと新しい明示承認を得るまではGitHub設定を変更しない。

スマホ／タブレット互換性を変えるCSS・生成器修正では`device_matrix.contract.json`を入力に`python3 device_matrix_check.py`を実行する。固定11条件を削って不具合を消さず、追加が必要なら契約とnegative testを同時に更新する。文字200%条件は320pxと390pxの両方を必須にし、実機OS挙動の完全再現ではなく、reflow回帰を検出するCSS文字寸法proxyとして扱う。printの幅判定はscreen viewportから分離し、A4相当794pxで行う。matrix reportは各profileの新規browser report、runner、ブラウザ検査器、preview server、base path設定、CSS、生成器、公開allowlist、契約のSHA-256、Chrome／Chromium version、各screenshotのSHA-256とPNG寸法、公開候補treeの前後SHA-256を持つ。`--base-url`利用時は配信中`build-report.json`と`--site-root`のSHA-256が一致しなければ描画前に停止する。古いreportの件数だけを現行コードの証拠に流用しない。

workflow、checker、`public_site.py`、browser verifier、tests、MathML registryを同じPRで変更すれば、同一repository内のcheckだけでは「人間が承認した意味変更」かを独立判定できない。これらgate-coreの変更は独立reviewを必要とし、validatorと期待値を同時に弱めてgreenにしない。承認数0のruleset案は、gate-coreがreview済みの時に省略・回帰を止める設計であり、同一PRによるvalidator自己改変の外部trust anchorではない。

## 実行後に報告する最小証拠

- 正本SHA、公開`build-report.json`のSHA、site commit SHA。
- 対象Actions run URLと結論。
- Markdown変換件数、HTML件数、内部リンク切れ件数、公開検疫結果。
- トップ200／不存在404と、代表ページの実ブラウザ確認範囲。
- 残るblocked/failed状態。未実行の検証を「確認済み」と書かない。
