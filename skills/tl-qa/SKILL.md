---
name: tl-qa
description: Use when checking translation quality after batch translation, or when the user says "tl-qa" or "翻訳QA"
---

# tl-qa（翻訳品質チェック）

`entries/*.jsonl` を機械検証し、レポートを `qa/` に出力する。機械検証で拾えない
訳質の問題は Claude が JSONL を読んで指摘する。

## プラグインルートの特定

このスキルの起動時に示される「Base directory for this skill」から `/skills/tl-qa` を
除いたパスが、このプラグイン（herma）のルートディレクトリ。以降「プラグインルート」
と書いたら、そのパスを指す（`${CLAUDE_PLUGIN_ROOT}` という環境変数は hooks 実行時にしか
展開されないため、コマンド実行時は実際の絶対パスに置き換えること）。

## 手順

1. **機械検証を実行** — `<PLUGIN_ROOT>` と `<PROJECT_DIR>` は実際の絶対パスに置き換えること
   ```bash
   uv run --project <PLUGIN_ROOT> python -c "
   import sys
   sys.path.insert(0, '<PLUGIN_ROOT>')
   from pathlib import Path
   from scripts import entries, validate, translate

   project = Path('<PROJECT_DIR>')
   cfg = translate.load_config(project / 'tl.config.json')
   glossary = {}
   for line in (project / 'glossary.tsv').read_text(encoding='utf-8').splitlines():
       if line.startswith('#') or not line.strip():
           continue
       parts = line.split('\t')
       if len(parts) >= 2:
           glossary[parts[0]] = parts[1]

   for path in sorted((project / 'entries').glob('*.jsonl')):
       rows = entries.load_jsonl(path)
       report = validate.run_validation(
           rows, cfg['placeholder_patterns'], glossary, cfg.get('max_len_ratio'),
           tgt_only_patterns=cfg.get('tgt_only_patterns', []),
       )
       entries.save_jsonl(path, rows)  # needs-review への降格を反映（対象は下記参照）
       out = project / 'qa' / f'{path.stem}-report.md'
       out.write_text(validate.render_report(report), encoding='utf-8')
       print(f'{path.name}: {len(report.violations)} 件の指摘 -> {out}')
   "
   ```
2. **検証項目**（`scripts/validate.py`）:
   - プレースホルダー保持（ゲームを壊す原因の第一位）。日本語化で新規に追加してよい
     タグ（ルビ記法など）があれば `tl.config.json` の `tgt_only_patterns` に登録する
   - 未訳検出
   - 用語集遵守（`src` 側は単語境界つきで照合するので部分文字列誤検知はしない）
   - 訳文の不統一（同一原文に異なる訳。未訳行は対象外）
   - 長さ超過（`max_len_ratio` 設定時）

   レポートは同一kind・同一メッセージの違反を件数付きでまとめ、id一覧は先頭20件まで
   表示する（超過分は「ほかN件」）。

   `status: needs-review` への降格は `translated`/`reviewed`/`needs-review` の行にのみ行う。
   `untranslated`/`stale` は翻訳待ちの正常状態なので、違反として報告はしても
   ステータスは変更しない（変更すると `tl-translate` の対象から永久に外れて
   しまうため）。`locked` は「正しいと表明済み」の凍結エントリなので検証対象外
   （報告もしない）。ただし不統一チェック（同一原文・別訳の検出）では
   ground truthとして比較に使われる。
3. **レポートを確認** — `qa/<ファイル名>-report.md` をユーザーに提示する。
   違反したエントリは `status: needs-review` に落ちているので、修正後は
   手順6の `apply` で `translated`/`reviewed` に戻す
4. **訳ゆれを統一** — レポートの `inconsistent`（同一原文・別訳）は、チャンクをまたいで
   同じ原文が別々に訳されると構造的に起きる。まずレポートだけ出す
   ```bash
   uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py unify <PROJECT_DIR>
   ```
   `qa/unify-report.md` をユーザーに見せ、了承を得てから `--fix` を付けて書き戻す。
   正とする訳は locked の最頻訳（locked が割れていて最頻訳の占有率が
   `unify_dominance_threshold`（既定0.9）未満なら据え置く）、locked が無ければ過半数、
   過半数も無ければid順で最初の行の訳。書き換えるのは `translated`/`needs-review` だけで、
   `reviewed`・`stale`・`locked` には触れない。擬音や文脈で訳し分ける語など、統一しては
   いけない原文は `tl.config.json` の `unify_skip_srcs` に列挙する
5. **機械検証で拾えない問題を指摘** — 口調の揺れ、不自然な訳、文脈に合わない訳語などは
   Claude が `entries/*.jsonl` を読んで気づいた点をユーザーに報告する
6. **修正の反映** — `entries/*.jsonl` を直接編集せず、修正を `[{"id": ..., "tgt": ..., "note": ...}]`
   （`note` は任意）のJSONファイルに書いて書き込む。未知のid・locked・プレースホルダー
   不一致・重複idは拒否され、旧訳は `prev_tgt` に退避される
   ```bash
   uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py apply <PROJECT_DIR> <edits.json> --dry-run
   ```
   拒否が無ければ `--dry-run` を外す。ユーザーが確認済みの修正なら `--status reviewed` を付ける

## 次のステップ

QAが完了したら `tl-inject` で元形式へ書き戻す。
