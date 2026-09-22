---
name: tl-extract
description: Use when writing an extraction adapter for a specific game's text format, or when the user says "tl-extract" or "原文を抽出"
---

# tl-extract（原文抽出アダプタの作成）

対象ゲーム/Modのテキスト形式を調査し、専用の抽出スクリプト（`scripts/extract.py`）を書いて
中間フォーマット（`entries/*.jsonl`）を生成する。

**重要**: 抽出元の形式はゲームごとに毎回異なる。このスキルは特定形式の実装を提供するのではなく、
アダプタをその都度書くための手順と参照実装を提供する。

## プラグインルートの特定

このスキルの起動時に示される「Base directory for this skill」から `/skills/tl-extract` を
除いたパスが、このプラグイン（herma）のルートディレクトリ。以降「プラグインルート」
と書いたら、そのパスを指す（`${CLAUDE_PLUGIN_ROOT}` という環境変数は hooks 実行時にしか
展開されないため、コマンド実行時は実際の絶対パスに置き換えること）。

## 手順

1. **対象ファイルを調査** — `source/` 配下の原文ファイルを確認し、形式を特定する
   （MToolのJSON辞書、XUnity.AutoTranslatorのTranslation.txt、Godotの翻訳CSV、独自バイナリ等）
2. **実装例を参照** — プラグインルートの `fixtures/fake_game/extract.py` を読む。
   ネストした構造を再帰的に走査して `{id, src, ctx}` の行を作る最小限のパターンを示している
3. **id設計を決める** — idは再抽出しても安定していること（配列インデックスではなく、
   ファイルパス・キー名・行番号など意味のある識別子にする）
4. **`scripts/extract.py` を書く** — 対象形式から `{id, src, ctx}` のリストを作る関数を実装する。
   `ctx` にはUI上の位置など、翻訳時に文脈として役立つ情報を入れる。
   会話・地の文がある形式では、次の2つを**別フィールドで**出す（`ctx` の文字列に埋め込まない）:
   - `scene` — 同じ場面として続けて読まれる行のまとまり（イベントページ・会話ファイル・
     章など）。`tl-translate` は scene ごとに行順のまま訳し、同じ場面の訳済み行・原作訳を
     文脈と手本として一緒に渡す。scene の無い行（UI文字列など）は同一原文をまとめて訳す
   - `speaker` — 話者名（分かる場合）。原作訳（locked）の同じ話者の台詞が口調の手本として渡る
   - 行は**ゲーム内の実行順**で出す（id でソートしない。`p10` が `p2` より前に来てしまう）。
     同じ scene の行は連続して出す（離れた位置に同じ scene が再び出ても別の場面として扱われる）。
     scene/speaker は再抽出のたびに既存エントリ（locked 含む）へ反映される
5. **中間フォーマットへマージ** — プラグインルートの `scripts/entries.py` の
   `load_jsonl` / `merge_extracted` / `save_jsonl` を使い、既存の `entries/*.jsonl` と
   マージする。新規idは`untranslated`、hashが変わったidは`stale`（旧訳は`prev_tgt`へ退避）、
   `locked`のidは常に凍結される。既に正しい訳が分かっている場合（公式ローカライズの流用、
   原語版から復元できる等）は、`{id, src, ctx}` に加えて `tgt`/`status` を渡せる。
   確定訳は `status: "locked"` で出すのを推奨する（QA検証の対象外になり、再抽出で
   原文が変わっても凍結されるため）。
   ただし「原作訳を復元した locked の枠を、MOD・ゲームの更新が別の原文に差し替える」
   ことがある対象では、`merge_extracted(existing, extracted, unfreeze_changed_locked=True)`
   を使う。原文が変わった locked も `stale`（旧訳は `prev_tgt`）になって再翻訳の対象に入る
   （凍結したままだと旧版の原文を抱えたまま残り、新版では原語のまま表示される）
6. **動作確認** — 抽出→（何も翻訳せず）`tl-inject` の逆変換で元ファイルと一致することを確認する
   （fixtureの往復テストと同じ考え方）

## 出力

`entries/*.jsonl` — 1ファイルにまとめても、シーン/チャプター単位で分割してもよい
（`tl-translate` はファイルごとに独立して処理する。ただし各ファイルの
Message Batchは完了まで最大24時間かかりうるうえ、`tl-translate`はファイルを逐次
待つため、細かく分割しすぎるとN回の直列待ちになる。数ファイル〜10ファイル程度に
まとめるのが実用的）。

## 次のステップ

抽出できたら `tl-translate` でバッチ翻訳、`tl-qa` で検証、`tl-inject` で書き戻す。
