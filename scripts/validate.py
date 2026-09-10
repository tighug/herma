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


def check_placeholders(src: str, tgt: str, patterns: list[str]) -> bool:
    """原文と訳文のトークンが多重集合として一致するか検証する。"""
    src_tokens = Counter(extract_tokens(src, patterns))
    tgt_tokens = Counter(extract_tokens(tgt, patterns))
    return src_tokens == tgt_tokens


def is_untranslated(entry: dict) -> bool:
    """訳文が空、または原文と完全一致しているかを判定する。"""
    tgt = entry["tgt"]
    return tgt == "" or tgt == entry["src"]


def check_glossary(src: str, tgt: str, glossary: dict[str, str]) -> bool:
    """原文に含まれる用語集の語について、対応する訳語が訳文にあるか検証する。"""
    for term, translation in glossary.items():
        if term in src and translation not in tgt:
            return False
    return True


def find_inconsistent_translations(rows: list[dict]) -> dict[str, list[str]]:
    """同一原文に対して異なる訳文が使われている箇所を検出する。未訳行は対象外。"""
    by_src: dict[str, list[str]] = {}
    for row in rows:
        if row["tgt"] == "":
            continue  # 未訳なだけの行を不統一として誤検知しない
        by_src.setdefault(row["src"], [])
        if row["tgt"] not in by_src[row["src"]]:
            by_src[row["src"]].append(row["tgt"])
    return {src: tgts for src, tgts in by_src.items() if len(tgts) > 1}


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
) -> Report:
    """全エントリを検証し、違反したものを needs-review に落としてレポートを返す。

    ステータスの変更は MUTABLE_ON_VIOLATION_STATUSES のエントリにのみ行う。
    untranslated/stale/locked は違反として報告はするが、ステータスは変更しない
    （変更すると翻訳待ちの行が永久にキューから外れてしまうため）。
    """
    report = Report()

    for row in rows:
        entry_id = row["id"]
        src, tgt = row["src"], row["tgt"]
        row_violations: list[Violation] = []

        if is_untranslated(row):
            row_violations.append(Violation(entry_id, "untranslated", "未訳です"))
        else:
            if not check_placeholders(src, tgt, patterns):
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


def render_report(report: Report) -> str:
    """検証結果をMarkdownレポートとして整形する。"""
    lines = ["# 翻訳QAレポート", ""]
    by_kind: dict[str, list[Violation]] = {}
    for v in report.violations:
        by_kind.setdefault(v.kind, []).append(v)

    if not by_kind:
        lines.append("問題は見つかりませんでした。")
        return "\n".join(lines)

    for kind, violations in by_kind.items():
        lines.append(f"## {kind}")
        for v in violations:
            lines.append(f"- [{v.entry_id}] {v.message}")
        lines.append("")

    return "\n".join(lines)
