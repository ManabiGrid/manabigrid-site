# ManabiGrid 展示サイト

[ManabiGrid（まなびグリッド）](https://github.com/ManabiGrid/manabigrid)の公開OSS教材を、GitHubに慣れていない人も読みやすい静的サイトへ変換します。教材の正本は別リポジトリに保ち、このリポジトリは生成器、検査器、公開用HTMLを管理します。

トップは特定学年を固定表示せず、正本の進捗表にある「中学／高校 × 5教科」の10入口を学習グリッドとして自動生成します。教材が実在する入口は今読める教科・学年へつなぎ、0件の入口も「準備中」として正本の単元骨格を示します。計画範囲は`curriculum/PROGRESS_INDEX.md`、掲載有無と件数は`materials/`から別々に取得するため、工程状態を「読める」と誤表示しません。

- 公開サイト: https://manabigrid.github.io/manabigrid-site/
- 正本: https://github.com/ManabiGrid/manabigrid
- 教材・図版: CC BY 4.0
- サイト生成器: MIT License

## 通常の更新はここから

別のCodexタスクやClaude Codeからでも、最初に次の読み取り専用コマンドを使います。正本、公開版、siteコード、日次更新の稼働状態を別々に返すため、モデルが「正本が同じだから全部最新」と推測する必要はありません。

```bash
python3 update_pages.py status
```

公開まで明示承認されている時だけ、同じrunnerへ`publish --approve-publication`を渡します。詳しい停止条件は[UPDATE_CONTRACT.md](UPDATE_CONTRACT.md)を正とし、検査失敗時に期待値を緩めたり、生成HTMLを直接直したりしません。

## 1コマンドでローカル再生成

Python 3の標準ライブラリだけを使います。追加パッケージは不要です。

```bash
python3 build_site.py
```

既定では、`MANABIGRID_SOURCE_ROOT`環境変数、隣接する`manabigrid/`、隣接するステージングcloneの順に正本を探します。別の場所にある場合は明示できます。

```bash
python3 build_site.py --source /path/to/manabigrid
```

この1コマンドで全対象Markdownを再変換し、学習グリッド、準備中の教科骨格、内部リンク、frontmatter非表示、`:::`ブロック、SVG、来歴、更新履歴、公開メタデータ、検索索引、公開検疫、正本HEADとの鮮度一致を検査します。件数は正本の更新に応じて変わります。更新履歴のため正本は履歴を省略しないGit cloneが必要です。

## ローカルで見る

相対リンクだけで閲覧する部分は`index.html`を直接開けます。GitHub Pagesのサブパスと404を含めて確かめるときは、ローカル専用サーバーを使います。

```bash
python3 preview_server.py
```

表示先は `http://127.0.0.1:8765/manabigrid-site/` です。外部インターフェースへbindしません。

## GitHubを初めて使う人へ

公開サイトの「このサイトについて」には、次を文字だけで案内しています。

1. READMEから読み始め、フォルダをたどる方法。
2. `materials/`の教材本文と`curriculum/`の進捗・単元一覧の違い。
3. アカウントなしの閲覧と、Code → Download ZIPによる保存。
4. Issueを作る3ステップと、個人情報を書かないための注意。
5. リポジトリ、コミット、Issue、Markdownのミニ辞典。

GitHubの画面変更で古くなりやすいスクリーンショットは使っていません。

## なぜ静的サイトか

現在必要なのは、正本の変換、閲覧、タイトル・見出し検索、前後移動、印刷、来歴表示です。ログイン、個人別進捗、投稿、データベース、サーバーAPIは必要ありません。

静的構成なら、検索語や学習履歴を外へ送らず、外部CDN、解析、トラッキング、Cookie、入力フォームを置かずに運用できます。正本との連動は動的サイト化ではなく、GitHub Actionsによる静的再ビルドで行います。サーバー側でなければ成立しない利用要件が具体化した時だけ動的化を再検討します。

## 正本更新との連動

`.github/workflows/pages.yml`は次の閉ループです。

1. 毎日または手動実行で正本の現在HEADを取得する。
2. 公開中の`build-report.json`と比較し、更新時だけ再生成する。
3. `build_site.py`、`check_site.py`、正本検疫と同等の生成物検疫を実行する。
4. 公開allowlistだけをPages artifactへまとめる。
5. すべて通った時だけGitHub Pagesへデプロイする。失敗時はdeploy jobを起動せず、直前の正常版を残す。

通常の正本更新は、このCodexタスクへ毎回依頼しなくても毎日03:17 JST以降の定期確認で反映されます。GitHub Actionsの混雑等により実際の開始は遅れることがあります。正本pushからサイトへの即時通知ではなく日次poll方式です。手動更新では承認した正本SHAを固定し、一意のrequest IDで該当runだけを追跡します。外部リンクのライブ到達性は毎回監視せず、公開前や大きな更新時だけ明示入力で実行します。public repositoryのscheduled workflowは長期間活動がないとGitHub側で無効化される場合があるため、手動実行も残しています。

別のCodexタスクやClaude Codeからでも、通常更新は次の同じ入口を使えます。モデルやeffortに依存する判断を減らすため、公式site origin・repository・base URL・正本URL、cleanなsite checkout、正本SHA、site SHA、対象run、公開後SHAとHTTP応答をスクリプトが固定・照合します。日次runが現在のsite HEADで動いたことも確認し、古いworkflowの成功を現行版の証明に流用しません。完全な契約と停止条件は`UPDATE_CONTRACT.md`です。

```bash
python3 update_pages.py status
python3 update_pages.py publish --approve-publication
```

`status`は`published_state`（公開レポートを正しく取得できたか）、`source_sync`（正本）、`site_sync`（生成器）、`release_readiness`（公開に使えるcheckoutか）、`operational_readiness`（日次workflowがactiveで最近動いたか）を分けて返します。公開レポートを取得・検証できない時は正本やsiteの差分を推測せず、`blocked_published_state_unknown`で停止します。正本SHAが公開版と同じでも、siteに未commit差分、未公開site commit、branch違い、origin drift、workflow契約違い、schedule無効化があればトップレベル状態をblockedにし、「すべて公開済み」と誤報しにくくしています。結果はignored `update-report.json`にも保存します。

ビルド自体も、公式ManabiGridをoriginに持つcleanな正本checkoutだけを受け付けます。未commit編集、fork、cleanだが未pushの正本HEADを正本コミット由来として表示せず、進捗表の不正行・罫線欠落・固定10入口の改変・可視単元名や学年群の不一致を生成側と独立検査側で停止します。

2行目は、現在の依頼でこのGitHub Pages更新が明示承認されている場合だけ実行します。フラグ自体は承認の代わりになりません。通常の正本更新だけならsiteのcommitは不要で、Actionsが隔離環境で生成します。生成器の互換修正が必要な時だけ、siteコードの検証・commit・pushを別レーンで行います。

正本SHAは同じまま、生成器・CSS・検査器だけをcommit／pushした更新では、push後に次の1コマンドで該当runと公開版を照合します。未公開site commitがある状態で`publish`を実行しても`already_current`にはせず、`blocked_site_release_requires_verification`で停止します。

```bash
python3 update_pages.py verify-site-release --site-sha <siteの40桁SHA> --source-sha <正本の40桁SHA>
```

公開`build-report.json`には正本commitに加えてsite commitも入り、別タスクや別モデルでも「どの生成器で作ったか」を機械確認できます。

トップページには最新3件、`updates/`には表示に影響する正本Git更新を新しい順で最大50件掲載します。正本の全履歴を取得してから、教材Markdown・図版・進捗・案内・権利情報に関わる変更だけを選びます。閲覧時にGitHub APIへ接続する機能ではなく、静的ビルド時の生成です。

すぐに反映したいとき、定期実行が失敗したとき、外部リンク検査や実描画まで確かめたいときは、このCodexタスクへ次のように依頼できます。

> ManabiGrid正本の現在HEADを確認し、展示サイトを再生成してください。正本は変更せず、全Markdown変換、リンク切れ、鮮度、公開検疫、モバイル／デスクトップ／ダークモードの実描画を確認し、全ゲート通過時だけ既存GitHub Pagesを更新してください。

## 公開artifact

リポジトリ全体はそのまま配信しません。`public_site.py`を単一allowlistとして、次だけをPages artifactへ入れます。

- ルート: `.nojekyll`、`index.html`、`404.html`、`robots.txt`、`sitemap.xml`、`build-report.json`
- ディレクトリ: `_assets/`、`_media/`、`about/`、`browse/`、`content/`、`curriculum/`、`progress/`、`subjects/`、`units/`、`updates/`

```bash
python3 package_site.py --dry-run
```

`check-report.json`、外部リンク検査結果、実描画スクリーンショット、印刷PDF、ロールバックアーカイブ、ローカルキャッシュは配信しません。

## 検査

```bash
python3 check_site.py . --source /path/to/manabigrid
python3 check_workflow.py
python3 device_matrix_check.py
python3 package_site.py --dry-run
```

端末マトリクスは、10条件それぞれのブラウザレポートと、検査runner・ブラウザ検査器・CSS・生成器・端末契約のSHA-256を`review/browser/device-matrix-report.json`へ記録します。文字200%条件は適用前後のroot/body文字サイズも実測し、単に「倍率を指定した」だけの成功扱いをしません。

外部URLの一回検査は明示opt-inです。対象URLを送信せずに「到達性」を確かめる方法はないため、通常ビルドから分離しています。

```bash
python3 check_external_links.py . --run
```

HTTP 404/410はhard broken、401/403/429や一時的なネットワーク失敗はblocked/unknownとして分けて記録します。

`device_matrix_check.py`はローカル専用サーバーを自動起動し、小型320pxスマホから大型412pxスマホ、横向きスマホ、600／768／820pxタブレット、1024×768px横向きタブレット、390pxで文字200%相当までの10条件を同じ12代表ページへ適用します。ページ全体の横はみ出し、局所スクロール、検索・進捗・404などの操作、主要導線、ダーク配色、印刷への復帰を検査します。文字200%はCSS文字寸法を拡大する回帰proxyであり、実機OSの文字拡大機能そのものを完全再現するものではありません。

## 主なファイル

- `build_site.py`: 再実行可能な静的サイト生成器
- `curriculum_grid.contract.json`: 中学／高校×5教科の安定URLとunit ID対応だけを固定する機械契約
- `check_site.py`: 全ページ、リンク、メタデータ、鮮度、SVG、MathML、公開検疫の検査器
- `check_external_links.py`: 明示実行する外部URL到達性検査器
- `preview_server.py`: GitHub Pagesのproject pathと404を再現するlocalhostサーバー
- `browser_check.py`: Chrome DevTools Protocolによる実描画検査
- `device_matrix.contract.json` / `device_matrix_check.py`: スマホ・タブレット・文字拡大の固定実描画マトリクス
- `check_workflow.py`: GitHub Actionsの構造・SHA pin・権限を検査
- `update_pages.py`: 承認SHA固定、run相関、公開後照合を行う通常更新の単一入口
- `UPDATE_CONTRACT.md`: Codex／Claude Code共通の更新・停止契約
- `package_site.py` / `public_site.py`: 公開allowlistとartifact生成
- `site.config.json`: 公開URL、対象リポジトリ、OG画像の単一設定
- `DESIGN.md`: 読者、視覚方針、レビュー採否、検証記録
- `MATH_RENDERING_ISSUE_DRAFT.md`: 正本側へ提案する数式3箇所のIssue草案（未起票）

## 既知の制限

- 中2数学「一次関数」は正本がL1〜L7の通しMarkdownなので1ページのままです。節ナビと現在地表示を追加しています。
- 表示数式3箇所のうち1箇所だけ静的MathMLを試作し、残り2箇所は意味を保つUnicode表示です。場所と方式を`build-report.json`へ記録します。
- project site配下の`robots.txt`は生成しますが、origin直下ではないため検索エンジンが必ず参照するとは限りません。canonicalとsitemapを主なURL手掛かりにします。
- 教材には候補ドラフトが含まれます。学校・公的機関の公式教材や公認サイトではありません。
