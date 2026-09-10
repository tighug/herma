---
name: tl-inject
description: Use when writing a translation write-back adapter for a specific game's text format, or when the user says "tl-inject" or "書き戻し"
---

# tl-inject（訳文の書き戻し）

`entries/*.jsonl` の訳文を、`tl-extract` で使った元の形式へ書き戻す専用スクリプト
（`scripts/inject.py`）を書き、`dist/` に配布物を作る。

**重要**: `tl-extract` の抽出処理の逆変換になる。抽出時に決めたid設計をそのまま使う。

## 手順

1. **実装例を参照** — `${CLAUDE_PLUGIN_ROOT}/fixtures/fake_game/inject.py` を読む。
   `extract.py` と対になる最小限のパターンを示している
2. **`scripts/inject.py` を書く** — `entries/*.jsonl` を読み、id をキーに元の構造へ
   訳文（`tgt`。空なら`src`のまま）を書き戻す関数を実装する
3. **`locked`/`needs-review` の扱いを確認** — `needs-review` のまま書き戻すと未修正の
   訳が配布されてしまうため、`tl-qa` の指摘が解消されているか事前に確認する
4. **出力** — `dist/` に元のゲーム形式のファイルとして書き出す
5. **往復確認** — 何も翻訳していない状態（全エントリ `untranslated`）で
   `extract` → `inject` を行い、元ファイルと構造が一致することを確認する
   （`fixtures/fake_game` の往復テストと同じ考え方）
6. **配布** — Mod形式で配布するか、原ファイル上書きかは対象ゲームによる。
   ユーザーに配布方法を確認してから `dist/` の内容を案内する

## 次のステップ

ゲームが更新されたら `tl-extract` を再実行し、hashが変わったエントリだけ
`stale` として再翻訳（`tl-translate`）→再QA（`tl-qa`）→再書き戻し、を繰り返す。
