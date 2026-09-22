# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## コマンド

```bash
uv sync
uv run pytest
uv run pytest tests/test_translate.py::test_apply_results_rejects_id_that_was_not_sent_in_this_chunk  # 単体テスト例
claude plugin validate .
```

このリポジトリをローカルパスのマーケットプレイスとして動作確認する場合、`claude plugin update` は
`plugin.json` の `version` を上げない限り再コピーしない。コードを変更したら
`claude plugin uninstall herma@herma` → `claude plugin install
herma@herma` で入れ替えること（詳細はREADME参照）。`plugin.json` と
`.claude-plugin/marketplace.json` の `version` は常に揃えて上げる。

## アーキテクチャ（2層構造）

- **ゲーム固有層**: `scripts/extract.py` / `scripts/inject.py` は**翻訳先プロジェクト側**に毎回書く
  アダプタで、このリポジトリには存在しない。`fixtures/fake_game/{extract,inject}.py` がその参照実装
  （かつ `tests/test_fake_game_roundtrip.py` の往復テスト対象）。
- **共通層（このリポジトリの本体）**: `scripts/entries.py`（JSONL入出力・hash差分マージ）、
  `scripts/translate.py`（Message Batches API ランナー）、`scripts/validate.py`（QA検証器）、
  `scripts/fix.py`（翻訳後の修正CLI: id指定の書き込み・prefill・訳ゆれ統一・status集計）、
  `scripts/translationese.py`（原作訳 locked を物差しにした翻訳調の測定。status は変えない）。
- **`skills/tl-*/SKILL.md` が唯一のユーザー導線**。`scripts/` は直接叩かれず、スキル手順書に
  埋め込まれた `uv run --project <PLUGIN_ROOT> ...` から呼ばれる。スキルの挙動を変えたら
  対応する `SKILL.md` の手順も必ず更新する。
- `${CLAUDE_PLUGIN_ROOT}` は hooks 実行時にしか展開されないため、SKILL.md 内のコマンドは
  絶対パスへの置換前提で書かれている。新しいスキルを足す際も同じ流儀に合わせる。
- `scripts/templates/CLAUDE.md` は**このファイルではない**。`tl-init` が翻訳先プロジェクトへ
  展開する「翻訳方針」テンプレートで、`tl-translate` がsystemプロンプトとして読み込む別物。

## 壊してはいけない不変条件

- **書き戻しは必ず `id` 照合、位置照合は禁止**（`scripts/translate.py` の `apply_results`）。
  送っていないidの応答は破棄し、欠落したidは未翻訳のまま据え置く。
- **`.tl/batch-<name>.json` が残っていれば新規投入せず同じバッチに再アタッチする**
  （`scripts/translate.py` の `run`）。バッチは最大24時間かかるため、再投入すると二重課金になる。
  再アタッチ時は `resolve_pending_sent_ids` が送信時hashと現在hashを突き合わせ、
  その間に原文が変わったidを「送信済み」から除外する。
- **`locked` エントリは原文が変わっても一切変更しない**（`scripts/entries.py` の `merge_extracted`）。
  例外は呼び出し側が `unfreeze_changed_locked=True` を明示したときだけで、そのときも原文が
  変わった locked を通常の hash 変化と同じく `stale` にする以外は変えない。`scripts/fix.py` の
  `apply` も locked への書き込みを拒否する。抽出位置のメタデータ `scene`/`speaker` だけは
  訳ではないので、locked でも再抽出のたびに置き換える（`_with_location_meta`）。
- **チャンクは行順（実行順）を崩さない**（`scripts/translate.py` の `build_chunks`）。
  `scene` のある行は同一原文で集約しない（場面が切れて直訳調に戻る）。チャンク内の参照行
  （locked/translated）は送信対象ではないので、`sent_ids`・状態ファイルには載せない。
- **QA違反による `needs-review` 降格は `MUTABLE_ON_VIOLATION_STATUSES` のステータスにのみ行う**
  （`scripts/validate.py`）。`untranslated`/`stale` を降格させると `select_translatable` の
  対象から永久に外れてしまう。`locked` も対象外。
- **`extract` 結果に無い既存idは削除しない** — `merge_extracted` はそのまま残す。
- ステータス遷移: `untranslated` / `translated` / `reviewed` / `stale` / `needs-review` / `locked`。

## テストの方針

- `scripts/translate.py` は純粋関数（対象選定・チャンク化・id照合・状態ファイルの読み書き）と、
  ネットワークI/O（`wait_for_batch` / `fetch_and_apply_results` / `run`）を意図的に分離している。
  新しいロジックは純粋関数側に置き、ネットワークなしでテストできる状態を保つ。
