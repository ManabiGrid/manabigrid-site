# Issue候補: 表示数式3箇所のMarkdown表現を再検討する

- status: owner-delivered-draft
- audience: maintainer
- external_send: not_performed

## 背景

展示サイトは外部CDNや数式ランタイムを使わない。正本の `$$...$$` 3箇所のうち1箇所を静的MathMLへ試作し、残る2箇所をUnicode・等幅文字で表示している。現行表示でも意味と数値は保持できているが、正本側で通常のMarkdown本文・表・SVGへ改稿できれば、GitHubと静的展示の双方で一貫して読みやすくなる。

## 対象（正本HEAD 7bc7571546b95bfe875a7c1300a4b6e9632b76bb）

1. `materials/jhs-math-3/jhs-math-3-similar-figures/lesson_06.md:27`
2. `materials/jhs-math-3/jhs-math-3-similar-figures/lesson_08.md:27`
3. `materials/jhs-math-3/jhs-math-3-similar-figures/lesson_10.md:28`

## サイト側の試作結果

3番の `MN∥BC,\quad MN=\frac{1}{2}BC` だけを、ビルド時に外部依存なしの静的MathMLへ変換した。分数を`mfrac`で表し、読み上げ名と元のTeX表現を`semantics`へ保持できた。汎用TeX変換器ではなく対象式との完全一致だけに限定しているため、教材本文を推測変換する危険は増やしていない。

試作はサイト側の表示改善として成立したが、正本Markdown上の一貫した表示を解決するものではない。そのため、正本側での表現再検討というIssue候補は維持する。

## 提案する検討

- 式の意味・数値・解法は変更しない。
- 通常のMarkdownだけで十分なら、複雑な数式記法を使わない表現へ改稿する。
- 図として示す方が認知負荷を下げる場合は、アクセシブルなSVGと本文説明の組み合わせを検討する。
- GitHub表示、展示サイト、白黒印刷の3条件で確認する。

この文面はサイトv5成果物としてリポジトリ所有者へ提出済みだが、GitHub Issueは起票していない。
