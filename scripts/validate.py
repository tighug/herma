"""JSONLエントリの品質検証（プレースホルダー保持・未訳・用語集・不統一・長さ超過）。"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


def extract_tokens(text: str, patterns: list[str]) -> list[str]:
    """テキストからプレースホルダー/タグ等のトークンを抽出する。"""
    tokens: list[str] = []
    for pattern in patterns:
        tokens.extend(re.findall(pattern, text))
    return tokens


def check_placeholders(
    src: str, tgt: str, patterns: list[str], tgt_only_patterns: list[str] = ()
) -> bool:
    """原文と訳文のトークンが多重集合として一致するか検証する。

    tgt_only_patterns にマッチするトークン（ルビ`\\RB[漢字,ルビ]`等、日本語化で
    新規に追加されてよいタグ）は、tgt側の比較対象から除外する。
    """
    src_tokens = Counter(extract_tokens(src, patterns))
    tgt_tokens = Counter(extract_tokens(tgt, patterns))
    for pattern in tgt_only_patterns:
        for token in re.findall(pattern, tgt):
            tgt_tokens[token] -= 1
            if tgt_tokens[token] <= 0:
                del tgt_tokens[token]
    return src_tokens == tgt_tokens


def is_untranslated(entry: dict) -> bool:
    """訳文が空、または原文と完全一致しているかを判定する。"""
    tgt = entry["tgt"]
    return tgt == "" or tgt == entry["src"]


def check_glossary(src: str, tgt: str, glossary: dict[str, str]) -> bool:
    """原文に含まれる用語集の語について、対応する訳語が訳文にあるか検証する。

    srcの照合は単語境界つき（"Home"が"Homeless Man"に誤検知しないように）。
    tgtの照合は部分文字列一致のまま（\bは日本語に対して無意味なため）。
    """
    for term, translation in glossary.items():
        pattern = re.compile(r"\b" + re.escape(term) + r"\b")
        if pattern.search(src) and translation not in tgt:
            return False
    return True


def find_inconsistent_translations(rows: list[dict]) -> dict[str, list[str]]:
    """同一原文に対して異なる訳文が使われている箇所を検出する。未訳行は対象外。

    locked（確定訳）はground truthとして比較には含めるが、locked同士だけの
    不一致は報告しない。ゲーム内語彙全体をlockedで一括投入するプロジェクトでは
    同一原文・別訳のlockedが大量に存在しうるため、そのノイズでレポートが埋もれる。
    non-locked行を1件でも含む原文グループのみ報告する。
    # ponytail: 同一原文にlockedが複数あり片方がnon-lockedと一致していてもグループ全体を報告する。
    #   locked同士の食い違いを個別に精査したくなったら分ける
    """
    by_src: dict[str, list[str]] = {}
    mutable_srcs: set[str] = set()
    for row in rows:
        if row["tgt"] == "":
            continue  # 未訳なだけの行を不統一として誤検知しない
        by_src.setdefault(row["src"], [])
        if row["tgt"] not in by_src[row["src"]]:
            by_src[row["src"]].append(row["tgt"])
        if row.get("status") != "locked":
            mutable_srcs.add(row["src"])
    return {
        src: tgts
        for src, tgts in by_src.items()
        if len(tgts) > 1 and src in mutable_srcs
    }


def check_length_ratio(src: str, tgt: str, max_ratio: float | None) -> bool:
    """訳文が原文比で長すぎないか検証する。max_ratioがNoneなら常に合格。"""
    if max_ratio is None:
        return True
    if len(src) == 0:
        return True
    return (len(tgt) / len(src)) <= max_ratio


@dataclass
class Violation:
    entry_id: str
    kind: str
    message: str


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)


# QAで違反が見つかった際に needs-review へ落としてよいステータス。
# untranslated/stale は翻訳待ちの正常状態、locked は意図的な凍結なので、
# ここに含めない（含めると tl-translate の対象から永久に外れてしまう）。
MUTABLE_ON_VIOLATION_STATUSES = {"translated", "reviewed", "needs-review"}


def run_validation(
    rows: list[dict],
    patterns: list[str],
    glossary: dict[str, str],
    max_len_ratio: float | None,
    tgt_only_patterns: list[str] = (),
) -> Report:
    """全エントリを検証し、違反したものを needs-review に落としてレポートを返す。

    ステータスの変更は MUTABLE_ON_VIOLATION_STATUSES のエントリにのみ行う。
    untranslated/stale は違反として報告はするが、ステータスは変更しない
    （変更すると翻訳待ちの行が永久にキューから外れてしまうため）。
    locked は「正しいと表明済み」の凍結エントリなので検証対象外
    （src/tgtの言語が異なる原文復元エントリ等ではプレースホルダーが
    構造上一致せず、大量の誤検知でレポートが機能しなくなるため）。
    """
    report = Report()

    for row in rows:
        if row["status"] == "locked":
            continue

        entry_id = row["id"]
        src, tgt = row["src"], row["tgt"]
        row_violations: list[Violation] = []

        if is_untranslated(row):
            row_violations.append(Violation(entry_id, "untranslated", "未訳です"))
        else:
            if not check_placeholders(src, tgt, patterns, tgt_only_patterns):
                row_violations.append(
                    Violation(entry_id, "placeholder", "プレースホルダーが原文と一致しません")
                )
            if not check_glossary(src, tgt, glossary):
                row_violations.append(Violation(entry_id, "glossary", "用語集の訳語が使われていません"))
            if not check_length_ratio(src, tgt, max_len_ratio):
                row_violations.append(Violation(entry_id, "length", "訳文が長すぎます"))

        if row_violations and row["status"] in MUTABLE_ON_VIOLATION_STATUSES:
            row["status"] = "needs-review"
        report.violations.extend(row_violations)

    for src, tgts in find_inconsistent_translations(rows).items():
        report.violations.append(
            Violation("(複数)", "inconsistent", f"{src!r} の訳が不統一: {tgts}")
        )

    return report


REPORT_MAX_IDS_PER_MESSAGE = 20


def render_report(report: Report) -> str:
    """検証結果をMarkdownレポートとして整形する。

    同一kind・同一messageの違反は「件数 + id一覧」にまとめる（1違反1行だと
    ゲーム全体をlockedで一括投入するプロジェクトで数十万行になり実用不能になるため）。
    id一覧は先頭REPORT_MAX_IDS_PER_MESSAGE件までとし、超過分は「ほかN件」と表記する。
    """
    lines = ["# 翻訳QAレポート", ""]
    by_kind: dict[str, dict[str, list[str]]] = {}
    for v in report.violations:
        by_kind.setdefault(v.kind, {}).setdefault(v.message, []).append(v.entry_id)

    if not by_kind:
        lines.append("問題は見つかりませんでした。")
        return "\n".join(lines)

    for kind, by_message in by_kind.items():
        lines.append(f"## {kind}")
        for message, entry_ids in by_message.items():
            shown = entry_ids[:REPORT_MAX_IDS_PER_MESSAGE]
            remaining = len(entry_ids) - len(shown)
            id_list = ", ".join(shown)
            if remaining > 0:
                id_list += f", ほか{remaining}件"
            lines.append(f"- {message} ({len(entry_ids)}件): {id_list}")
        lines.append("")

    return "\n".join(lines)
