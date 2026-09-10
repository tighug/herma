---
name: tl-init
description: Use when starting a new game/mod translation project, or when the user says "tl-init" or "翻訳プロジェクトを初期化"
---

# tl-init（翻訳プロジェクト初期化）

ゲーム翻訳プロジェクトのディレクトリ構造と設定雛形を、対象ディレクトリに展開する。

## 手順

1. **対象ディレクトリを確認** — 引数で指定されていなければユーザーに聞く（新規ディレクトリでも既存でもよい）
2. **ゲーム情報をヒアリング** — 以下をユーザーに確認する
   - ゲーム名
   - 原文の言語・訳文の言語（デフォルト: en → ja）
   - プレースホルダーの記法（例: `{playerName}`、`%s`、`<color=...>`タグなど）。`fixtures/fake_game/dialogue.json` の例を見せて説明するとよい
3. **雛形を展開** — `${CLAUDE_PLUGIN_ROOT}/scripts/templates/` の各ファイルを対象ディレクトリにコピーする
   - `tl.config.json` — ヒアリング内容で `<GAME_NAME>` 等のプレースホルダーを埋める
   - `CLAUDE.md` — ゲーム名を埋め、翻訳方針は空欄のまま（後で `tl-extract`/翻訳中に育てていく）
   - `glossary.tsv`
   - `gitignore` → `.gitignore` としてコピー
4. **ディレクトリを作成** — `source/`, `entries/`, `scripts/`, `dist/`, `qa/`, `.tl/` を空ディレクトリとして用意する
5. **確認** — 生成した `tl.config.json` の内容をユーザーに提示し、間違いがないか確認する

## 生成後のディレクトリ構造

```
<対象ディレクトリ>/
├── CLAUDE.md
├── tl.config.json
├── glossary.tsv
├── .gitignore
├── source/
├── entries/
├── scripts/
├── dist/
├── qa/
└── .tl/
```

## 次のステップ

雛形ができたら `tl-extract` に進み、実際のゲームファイルから抽出アダプタを書く。
