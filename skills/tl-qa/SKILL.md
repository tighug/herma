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
   手順7の `apply` で `translated`/`reviewed` に戻す
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
6. **翻訳調を測る**（英→日。原作の訳が `locked` として十分あるプロジェクト向け）
   ```bash
   uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/translationese.py <PROJECT_DIR>
   ```
   英語原文にその語（she/her・just・so/very・please…）がある行だけを母数にして、訳文にその訳語
   （彼女・ただ・とても・お願い…）が出る率を、原作（`locked`）と機械翻訳（locked 以外で訳がある行）で
   比べる。`qa/translationese-report.md` と `qa/translationese.json`（`tags` = 行ごとのタグ）を出す。
   **測るだけで status は変えない**（手順1の降格とは別経路）。
   - **採用**（機械翻訳が原作の2倍以上・5pt以上高い・母数が両側とも30行以上）の語は、機械翻訳が
     原作より律儀に訳している語。`tags` の行を場面ごとに書き直し、手順7で反映する
   - **原作が避ける**（原作の率15%以下）の語は、翻訳前のプロジェクトでも出る。`CLAUDE.md` の
     翻訳方針に「訳さずに済ませる語」として `exemplars`（原作の手本）と一緒に書くと、
     `tl-translate` の段階で翻訳調を予防できる（直すより予防のほうが安い）
   - 語のリストは一般語だけ。罵倒語・喘ぎ・擬音など作品に固有の語は `tl.config.json` の
     `translationese.extra_calques`（`[キー, 英語の正規表現, 訳文の正規表現]` の配列）で足し、
     名前・説明文など語の選び方が違う枠は `translationese.non_prose_id_prefixes` で外す
   - 書き直すときの落とし穴は下の「翻訳調を直すときの落とし穴」を必ず読む
7. **修正の反映** — `entries/*.jsonl` を直接編集せず、修正を `[{"id": ..., "tgt": ..., "note": ...}]`
   （`note` は任意）のJSONファイルに書いて書き込む。未知のid・locked・プレースホルダー
   不一致・重複idは拒否され、旧訳は `prev_tgt` に退避される
   ```bash
   uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py apply <PROJECT_DIR> <edits.json> --dry-run
   ```
   拒否が無ければ `--dry-run` を外す。ユーザーが確認済みの修正なら `--status reviewed` を付ける

## 翻訳調を直すときの落とし穴

実際の作品（約8,000行の書き直し）で踏んだもの。

- **全体の出現率で比べない。** 機械翻訳の行と原作の行では文の種類（会話・三人称のログ）の割合が違い、
  翻訳調と混ざる。必ず「英語原文にその語がある行」に条件を揃える（`translationese.py` はそうしている）
- **数値は作品ごとに測る。** 他の作品の CLAUDE.md の率をコピーしない。語り口が違えば手本の率も違う。
  原作の母数が小さい語（数十行）は物差しにならない
- **書き直すと別の一語へ寄る。** 「彼女の髪」を消すと「その髪」が増え、「感じる」は「味わう」へ、
  罵倒語は別の一語へ集まる。書き直した後に必ず測り直し、置き換え先の率も原作と比べる
  （置き換え先の語は `extra_calques` に入れておく）。倍率が低くても差が大きい偏りは閾値に
  掛からないので、手で散らした語は1語ずつ測る
- **1つの軸を直すと別の軸が悪くなる。** 読点を減らすと「〜ている」が増える、など。LLM の書き直し案は、
  元の訳よりタグ（採用語・読点・〜ている）が増えるなら機械的に捨てる
- **LLM の書き直しは適用前に機械で弾く。** 元の訳に無い制御文字の追加・欠落、行数の変化、
  作業メモの混入、方針で禁じた記号（句点など）の再導入、用語集の訳語の消失
- **原作と同じ (src, tgt) の行は直さない。** 原作の言い回しそのもの（`translationese.py` はタグを付けない）
- **向きが逆の比較をしない。** 原作が「日本語 → 英語」に訳されている作品では、英語原文の記号
  （`~` など）は日本語を写したもの。英語の記号の有無で原作と機械翻訳を比べても意味がない
- **擬音・喘ぎなど文体の規則は作品側に書く。** 原作が多用する表現を減らす方向の書き直しは、
  翻訳調を直すつもりで原作から遠ざかることがある。減らす前に原作の率を測る

## 次のステップ

QAが完了したら `tl-inject` で元形式へ書き戻す。
