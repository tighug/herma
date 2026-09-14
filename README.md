# herma

ゲーム/Mod翻訳を汎用的に進めるための Claude Code プラグイン。

## 設計思想

対象ゲームの形式は毎回バラバラなので、**再利用できるのは中間層だけ**と割り切っている。

- **抽出（extract）・書き戻し（inject）はゲームごとに毎回書く**アダプタ（`tl-extract`/`tl-inject`
  スキルが作成を支援する）
- **中間フォーマット（JSONL）・バッチ翻訳・品質検証・用語集**はこのプラグインが提供する共通部分

原文が更新されたときは、変更されたエントリだけをハッシュ差分で検出して `stale` にし、
旧訳を `prev_tgt` に残したまま再翻訳できる。これが一度きりの作業ではなく「環境」たる所以。

## インストール

翻訳対象のゲームごとに別リポジトリ（例: `tl_<game>`）を用意し、そこにこのプラグインを入れる。

```
/plugin marketplace add https://github.com/tighug/herma
/plugin install herma
```

## スキル

| スキル         | 役割                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------- |
| `tl-init`      | 新規ゲーム翻訳プロジェクトの雛形（`tl.config.json`・`CLAUDE.md`・ディレクトリ構造）を展開する |
| `tl-extract`   | 対象ゲームのテキスト形式を調査し、抽出アダプタ（`scripts/extract.py`）を書く                  |
| `tl-translate` | Message Batches API で未翻訳/staleなエントリを一括翻訳する                                    |
| `tl-qa`        | プレースホルダー保持・用語集遵守・訳文の不統一・長さ超過を機械検証する                        |
| `tl-inject`    | 訳文を元の形式へ書き戻すアダプタ（`scripts/inject.py`）を書く                                 |

## 中間フォーマット

`entries/*.jsonl`、1エントリ1行:

```json
{
  "id": "scene01/0012",
  "src": "Hello, {playerName}!",
  "tgt": "やあ、{playerName}！",
  "ctx": "村人に話しかける場面",
  "status": "translated",
  "hash": "a1b2c3d4",
  "prev_tgt": null,
  "note": ""
}
```

`status` は `untranslated` / `translated` / `reviewed` / `stale` / `needs-review` / `locked` を遷移する。

## 前提条件

`tl-translate` は Anthropic Message Batches API を使う。以下のいずれかが必要:

- `ANTHROPIC_API_KEY` を環境変数に設定
- `ant auth login` で認証（`ant` CLIが必要）

料金は `claude-opus-5` のBatches API利用時で、入力 $2.50/1M tok、出力 $12.50/1M tok
（通常価格の50%）。モデルは各プロジェクトの `tl.config.json` で指定する。

## 開発

```bash
uv sync
uv run pytest
```

`scripts/` 配下の共通ロジック（JSONL入出力・hash差分・バッチ翻訳のid照合・検証器）は
`tests/` でユニットテストされている。`fixtures/fake_game/` は実ゲームに依存しない
往復テスト（extract→translate→inject でプレースホルダーが保持されること等）に使う
架空ゲームデータで、`tl-extract`/`tl-inject` の実装例も兼ねる。

### プラグインの動作確認（ローカル）

```bash
claude plugin marketplace add /path/to/herma
claude plugin install herma@herma
claude plugin validate .
```

**注意**: このリポジトリをローカルパスのマーケットプレイスとして使う場合、
`claude plugin update` は `plugin.json` の `version` を上げない限り再コピーしない。
コードを変更したら `claude plugin uninstall herma@herma` →
`claude plugin install herma@herma` で最新のコピーに入れ替えること
（さもないと動作確認しているのが古いコードのままになる）。
