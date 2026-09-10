---
name: tl-qa
description: Use when checking translation quality after batch translation, or when the user says "tl-qa" or "翻訳QA"
---

# tl-qa（翻訳品質チェック）

`entries/*.jsonl` を機械検証し、レポートを `qa/` に出力する。機械検証で拾えない
訳質の問題は Claude が JSONL を読んで指摘する。

## 手順

1. **機械検証を実行** —
   ```bash
   uv run --project "${CLAUDE_PLUGIN_ROOT}" python -c "
   import sys, json
   sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}')
   from pathlib import Path
   from scripts import entries, validate, translate

   project = Path('<対象プロジェクトディレクトリ>')
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
       entries.save_jsonl(path, rows)  # needs-review への降格を反映
       out = project / 'qa' / f'{path.stem}-report.md'
       out.write_text(validate.render_report(report), encoding='utf-8')
       print(f'{path.name}: {len(report.violations)} 件の指摘 -> {out}')
   "
   ```
2. **検証項目**（`scripts/validate.py`）:
   - プレースホルダー保持（ゲームを壊す原因の第一位）
   - 未訳検出
   - 用語集遵守
   - 訳文の不統一（同一原文に異なる訳）
   - 長さ超過（`max_len_ratio` 設定時）
3. **レポートを確認** — `qa/<ファイル名>-report.md` をユーザーに提示する。
   違反したエントリは `status: needs-review` に落ちているので、修正後は
   ステータスを手動で `translated`/`reviewed` に戻す
4. **機械検証で拾えない問題を指摘** — 口調の揺れ、不自然な訳、文脈に合わない訳語などは
   Claude が `entries/*.jsonl` を読んで気づいた点をユーザーに報告する
5. **修正の反映** — ユーザーの指示に応じて `tgt` を直接編集し、`status` を更新する

## 次のステップ

QAが完了したら `tl-inject` で元形式へ書き戻す。
