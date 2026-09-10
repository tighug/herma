# ゲーム翻訳用 Claude Code プラグイン 設計書

- 日付: 2026-09-11
- ステータス: 承認済み

## 背景

ユーザーは過去に複数のゲーム/Mod翻訳プロジェクト（`tl_phonia_chmod`、`celesphonia` 系、`grimshire`、`mercuria` など）を、それぞれ別リポジトリで、その都度 superpowers プラグインを入れながら進めてきた。毎回ゼロから手順・ディレクトリ構成・品質チェックを組み直しており、再利用可能な土台がない。

この `game-translation` リポジトリ（当初は空）を **Claude Code プラグイン**として構築し、各ゲームの翻訳リポジトリから `/plugin install` して使える共通基盤にする。

## ブレストで確定した前提

| 項目 | 決定 |
| --- | --- |
| このリポジトリの役割 | 共通ツール置き場（プラグイン本体）。翻訳作業自体は従来通り別リポジトリ |
| 対象フォーマット | 毎回バラバラ。共通化するのは中間層のみ |
| 翻訳の進め方 | 一括バッチ翻訳 → 後でまとめてQA |
| バッチ実行方式 | Anthropic Message Batches API を叩く Python スクリプト |
| スクリプト言語 | Python（`uv` 利用） |

設計の核心は **「再利用できるのは中間層だけ」**。抽出（extract）と書き戻し（inject）はゲーム専用アダプタとして毎回書き捨て、その間にある中間フォーマット・翻訳ランナー・検証器・用語集をプラグインが提供する。

## 前提条件

このマシンには `ANTHROPIC_API_KEY` も `ant` CLI も存在しない。Batches API を使うには以下のいずれかが必要:

- `ANTHROPIC_API_KEY` を発行して環境変数に設定
- または `ant` CLI をインストールして `ant auth login`

料金の目安（`claude-opus-5`、Batches API は全トークン 50% 割引）:

| | 通常 | Batch |
| --- | --- | --- |
| 入力 $/1M tok | $5.00 | $2.50 |
| 出力 $/1M tok | $25.00 | $12.50 |

モデルは `tl.config.json` のフィールドとし、既定は `claude-opus-5`。コスト都合で勝手に下げない。

## プラグインのディレクトリ構成

```
game-translation/
├── .claude-plugin/
│   ├── plugin.json          # name: game-translation, version, description
│   └── marketplace.json     # plugins: [{ name, source: "./" }] で自己ホスト
├── skills/
│   ├── tl-init/SKILL.md
│   ├── tl-extract/SKILL.md
│   ├── tl-translate/SKILL.md
│   ├── tl-qa/SKILL.md
│   └── tl-inject/SKILL.md
├── scripts/                 # 共通Pythonコード
│   ├── entries.py           # JSONL入出力・hash計算・status遷移
│   ├── translate.py         # Batches API ランナー
│   ├── validate.py          # プレースホルダー/長さ/用語集検証
│   └── templates/           # tl-init が撒く雛形一式
│       ├── tl.config.json
│       ├── CLAUDE.md
│       ├── glossary.tsv
│       └── gitignore
├── fixtures/                # 検証用の架空ゲームデータ
├── tests/                   # scripts/ のユニットテスト
└── README.md
```

スキル名に `tl-` を付けるのは、ユーザーの個人スキル（`ingest`/`lint`/`query`/`research`）および Claude Code 組み込みの `/init` と確実に衝突させないため。プラグイン名前空間に頼らず名前自体で分離する。

参照実装は superpowers プラグイン（`.claude-plugin/marketplace.json` の `"source": "./"` による自己ホスト形式、`${CLAUDE_PLUGIN_ROOT}` の使い方）から踏襲する。

## 中間フォーマット（JSONL）

`entries/*.jsonl`、1エントリ1行。翻訳の唯一の正データ。

```json
{"id":"scene01/0012","src":"Hello, {playerName}!","tgt":"やあ、{playerName}！","ctx":"村人に話しかける場面","status":"translated","hash":"a1b2c3d4","prev_tgt":null,"note":""}
```

| フィールド | 意味 |
| --- | --- |
| `id` | 元データ内での一意キー。抽出元のキーやパスをそのまま使う |
| `src` / `tgt` | 原文 / 訳文 |
| `ctx` | 文脈情報（話者・場面・UI上の位置など）。任意 |
| `status` | `untranslated` / `translated` / `reviewed` / `stale` / `needs-review` / `locked` |
| `hash` | `src` のハッシュ。ゲーム更新時の差分検出に使う |
| `prev_tgt` | 原文が変わった際の旧訳。捨てずに残す |

**原文更新時の扱い**: 再抽出して `hash` が変わったエントリは、`tgt` を `prev_tgt` に退避して `status: stale` にする。訳文は消さない。再翻訳時のプロンプトに旧原文・旧訳を添えることで、差分だけを最小コストで直せる。`status: locked` のエントリは再翻訳対象から常に除外する。

## ゲーム側プロジェクトの構造（`tl-init` が生成）

```
tl_<game>/
├── CLAUDE.md          # このゲーム固有の翻訳方針（文体・一人称・敬語・固有名詞）
├── tl.config.json     # モデル/言語/プレースホルダー記法/チャンクサイズ
├── glossary.tsv       # 原文 <TAB> 訳文 <TAB> 備考
├── source/            # 抽出元の生ファイル（gitignore）
├── entries/           # *.jsonl（翻訳の正データ。ここだけは必ずコミット）
├── scripts/
│   ├── extract.py     # このゲーム専用アダプタ（書き捨て）
│   └── inject.py
├── dist/              # 書き戻し済みの配布物（gitignore）
├── qa/                # QAレポート出力先
└── .tl/               # バッチID等の実行状態（gitignore）
```

`tl.config.json` の主なフィールド:

```json
{
  "game": "Grimshire",
  "source_lang": "en",
  "target_lang": "ja",
  "model": "claude-opus-5",
  "chunk_size": 30,
  "placeholder_patterns": ["\\{[^}]*\\}", "%[sd]", "<[^>]+>", "\\\\n"],
  "max_len_ratio": null
}
```

`placeholder_patterns` はゲームごとに違うため設定で持つ。これが検証器の入力になる。

## スキルの役割

| スキル | やること |
| --- | --- |
| `tl-init` | `scripts/templates/` の雛形を対象ディレクトリに展開し、ゲーム名・言語・プレースホルダー記法をユーザーに聞いて `tl.config.json` を埋める |
| `tl-extract` | 対象ファイルを調査し、そのゲーム専用の `scripts/extract.py` を書く。`fixtures/` の実装例を参照する。出力は `entries/*.jsonl`。再実行時は既存 JSONL とマージし、hash 差分を `stale` にする |
| `tl-translate` | `tl.config.json` を読み、プラグインの `scripts/translate.py` を実行する。バッチ投入 → 完了待ち → 書き戻しまでを見届ける |
| `tl-qa` | `scripts/validate.py` を実行し、レポートを `qa/` に出力。機械検証で拾えない訳質の問題（口調の揺れ等）は Claude が JSONL を読んで指摘する |
| `tl-inject` | `scripts/inject.py` を書き、`entries/` から元形式へ書き戻して `dist/` を作る。抽出の逆変換であることを往復テストで確認する |

## `scripts/translate.py` の設計

Message Batches API を使う。

- **対象選定**: `status` が `untranslated` または `stale` のエントリのみ。`locked` は必ず除外。
- **チャンク化**: `chunk_size` 件ずつを1リクエストにまとめる。`custom_id` はチャンク番号。
- **system プロンプト**（全リクエスト共通）: `CLAUDE.md` の翻訳方針 + `glossary.tsv` + プレースホルダー保持ルール。末尾ブロックに `cache_control: {"type": "ephemeral", "ttl": "1h"}` を付ける。
- **出力形式**: `output_config={"format": {"type": "json_schema", "schema": ...}}` で `[{id, tgt}]` を強制する。バッチ経路では `client.messages.parse()` は使えないため、返ってきたテキストを自前で `json.loads` する。受け付けられなければプロンプト指示 + 防御的パースにフォールバックする。
- **完了待ち**: `batches.retrieve(id).processing_status == "ended"` までポーリング。バッチIDを `.tl/batch-<timestamp>.json` に保存し、中断しても再課金なしで再アタッチできるようにする。
- **書き戻し**: `batches.results(id)` は順不同で返るため `custom_id` でキー引きする。

**id 照合は二重に行う（最重要）**。バッチ結果は `custom_id` で、チャンク内の各行は `id` で突き合わせる。位置で書き戻すことは絶対にしない。

- そのチャンクが送っていない `id` が返ってきた → 破棄してログに記録
- 送ったのに返ってこない `id` → `untranslated` のまま据え置き

これがパイプライン全体で最も起きやすい静かなデータ破壊であり、ここだけはテストを先に書く。

## `scripts/validate.py` の検証項目

| 検証 | 内容 |
| --- | --- |
| プレースホルダー保持 | `placeholder_patterns` で `src`/`tgt` からトークンを抽出し、多重集合として一致するか。**ゲームを壊す原因の第一位** |
| 未訳検出 | `tgt` が空、または `src` と完全一致 |
| 用語集遵守 | `src` に用語集の原語を含むエントリの `tgt` に、対応する訳語があるか |
| 訳文の不統一 | 同一 `src` に対し異なる `tgt` が存在する |
| 長さ超過 | `max_len_ratio` 設定時、`tgt` が原文比で長すぎる（固定幅UIのはみ出し対策） |

出力は `qa/report-<date>.md`。違反したエントリは `status: needs-review` に落とす。

## 検証方法（fixture による往復テスト）

対象ゲームが毎回異なり、実ゲームで検証できないため、**プラグイン内に架空ゲームの fixture をコミットする**。これが中間層の動作保証であり、同時に `tl-extract` が実アダプタを書くときに参照する実装例にもなる。

`fixtures/` に置くもの:

- `fake_game/dialogue.json` — ネストしたキー構造、`{playerName}` 形式のプレースホルダー、`<color=#ffffff>` タグ、`\n` を含む英語テキスト
- `fake_game/extract.py` / `inject.py` — その fixture 用のアダプタ例

通すテスト:

1. **往復**: extract → inject して、翻訳前なら元ファイルとバイト一致すること
2. **プレースホルダー保存**: 訳文を流し込んだ後も `{playerName}`・`<color=…>`・`\n` が全て残っていること
3. **id 不整合の拒否**: 送っていない `id` を含む偽のAPI応答を食わせ、書き戻されずログに落ちること
4. **hash 差分**: `src` を1行書き換えて再抽出し、そのエントリだけが `stale` になり `prev_tgt` に旧訳が退避されること。他エントリが `translated` のままであること
5. **検証器**: プレースホルダーを1つ落とした訳文を `validate.py` が検出すること

API を実際に叩くのは、上記が全て通った後の小規模な1バッチのみ（キャッシュ効果の測定を兼ねる）。
