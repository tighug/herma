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
           rows, cfg['placeholder_patterns'], glossary, cfg.get('max_len_ratio')
       )
       entries.save_jsonl(path, rows)  # needs-review への降格を反映（対象は下記参照）
       out = project / 'qa' / f'{path.stem}-report.md'
       out.write_text(validate.render_report(report), encoding='utf-8')
       print(f'{path.name}: {len(report.violations)} 件の指摘 -> {out}')
   "
   ```
2. **検証項目**（`scripts/validate.py`）:
   - プレースホルダー保持（ゲームを壊す原因の第一位）
   - 未訳検出
   - 用語集遵守
   - 訳文の不統一（同一原文に異なる訳。未訳行は対象外）
   - 長さ超過（`max_len_ratio` 設定時）

   `status: needs-review` への降格は `translated`/`reviewed`/`needs-review` の行にのみ行う。
   `untranslated`/`stale` は翻訳待ちの正常状態なので、違反として報告はしても
   ステータスは変更しない（変更すると `tl-translate` の対象から永久に外れて
   しまうため）。`locked` は「正しいと表明済み」の凍結エントリなので検証対象外
   （報告もしない）。ただし不統一チェック（同一原文・別訳の検出）では
   ground truthとして比較に使われる。
3. **レポートを確認** — `qa/<ファイル名>-report.md` をユーザーに提示する。
   違反したエントリは `status: needs-review` に落ちているので、修正後は
   ステータスを手動で `translated`/`reviewed` に戻す
4. **機械検証で拾えない問題を指摘** — 口調の揺れ、不自然な訳、文脈に合わない訳語などは
   Claude が `entries/*.jsonl` を読んで気づいた点をユーザーに報告する
5. **修正の反映** — ユーザーの指示に応じて `tgt` を直接編集し、`status` を更新する

## 次のステップ

QAが完了したら `tl-inject` で元形式へ書き戻す。
