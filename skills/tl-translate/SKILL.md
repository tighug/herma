---
name: tl-translate
description: Use when running batch translation over extracted entries, or when the user says "tl-translate" or "一括翻訳"
---

# tl-translate（一括バッチ翻訳）

`entries/*.jsonl` の未翻訳・stale なエントリを Anthropic Message Batches API で
一括翻訳し、結果を書き戻す。

## プラグインルートの特定

このスキルの起動時に示される「Base directory for this skill」から `/skills/tl-translate` を
除いたパスが、このプラグイン（game-translation）のルートディレクトリ。以降「プラグインルート」
と書いたら、そのパスを指す（`${CLAUDE_PLUGIN_ROOT}` という環境変数は hooks 実行時にしか
展開されないため、コマンド実行時は実際の絶対パスに置き換えること）。

## 前提

- `ANTHROPIC_API_KEY` または `ant auth login` の認証情報が必要
- 未設定なら、キーの発行方法（console.anthropic.com）と `ant auth login` の
  どちらかをユーザーに案内する。推測でキーを要求しない
- `tl.config.json` の `model` に設定されたモデルを使う（勝手に安いモデルへ変更しない）

## 手順

1. **設定確認** — `tl.config.json` の `model` / `chunk_size` / `placeholder_patterns` を確認する
2. **対象件数の見積もり** — プラグインルートの `scripts/translate.py` の
   `select_translatable` で対象件数（`untranslated`/`stale`）を数え、概算コストを
   ユーザーに提示してから実行の可否を確認する
3. **実行** — `<PLUGIN_ROOT>` は実際のプラグインルートの絶対パスに置き換えること
   ```bash
   uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/translate.py <対象プロジェクトディレクトリ>
   ```
   内部で行われること:
   - `CLAUDE.md`（翻訳方針）+ `glossary.tsv`（用語集）+ プレースホルダー規則を
     systemプロンプトとしてキャッシュ付きで組み立てる
   - `chunk_size` 件ずつチャンク化してバッチ投入（`custom_id: chunk-N`）
   - 投入直後にバッチIDと送信id一覧を `.tl/batch-<entriesファイル名>.json` へ保存する。
     次回このコマンドを実行したとき、同じファイルに対応する状態ファイルが残っていれば
     **新規投入せずそのバッチに再アタッチする**（バッチは最大24時間かかるため、
     中断のたびに新規投入すると二重課金になる）
   - 完了までポーリングし、完了したら結果を `custom_id`（チャンク）と `id`（行）の
     二重照合で書き戻す。**位置での書き戻しは行わない** — 送っていないidの応答は破棄し、
     応答が欠落したidは未翻訳のまま残る。書き戻しが終わった状態ファイルは削除される
4. **結果を報告** — 翻訳件数、破棄件数、欠落件数、失敗チャンク、キャッシュ実測
   （`cache_read_input_tokens`/`cache_creation_input_tokens`）をユーザーに伝える。
   キャッシュのreadが0のままなら、systemプロンプトに揺れる内容（日時など）が
   混入していないか確認する
5. **`tl-qa` へ** — 翻訳後は必ず `tl-qa` で品質チェックを行う

## stale エントリの再翻訳について

原文が更新されて `stale` になったエントリは、`prev_tgt`（旧訳）も一緒にモデルへ渡し、
差分だけを踏まえた再翻訳を促す。
