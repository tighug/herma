"""翻訳後の修正ツール: id指定の書き込み・prefill・訳ゆれ統一・status集計。

どれも「entries/*.jsonl を全て読み、id で照合して書き換え、変わったファイルだけ保存する」
形をとる。書き換えロジックはネットワーク・ファイルに依存しない純粋関数で、main() は
その薄いCLIラッパー。

    uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py status  <PROJECT>
    uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py apply   <PROJECT> <edits.json> [--status reviewed] [--dry-run]
    uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py prefill <PROJECT> [--dry-run]
    uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/fix.py unify   <PROJECT> [--fix]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# translate.py と同じ理由（直接実行時に `from scripts import ...` を解決するため）
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from scripts import entries as entries_mod  # noqa: E402
from scripts.validate import check_placeholders, has_translatable_text  # noqa: E402

PREFILLABLE_STATUSES = {"untranslated", "stale"}
# 訳ゆれ統一で書き換えてよいステータス。reviewed は人が確認済み、stale は旧原文への訳、
# locked は確定訳なので触れない。
UNIFIABLE_STATUSES = {"translated", "needs-review"}
DEFAULT_DOMINANCE_THRESHOLD = 0.9


def apply_edits(
    rows: list[dict],
    edits: list[dict],
    patterns: list[str],
    tgt_only_patterns: list[str] = (),
    status: str = "translated",
) -> tuple[list[str], list[tuple[str, str]]]:
    """edits（[{id, tgt, note?}]）を id 照合で rows へ書き込む。rows はその場で更新する。

    未知のid・locked・プレースホルダー不一致・edits内で重複したidは書き込まず、
    (id, 理由) として返す。書き込んだ行の旧訳は prev_tgt へ退避する。
    """
    by_id = {r["id"]: r for r in rows}
    id_counts = Counter(e["id"] for e in edits)
    applied: list[str] = []
    rejected: list[tuple[str, str]] = []

    for edit in edits:
        entry_id, tgt = edit["id"], edit["tgt"]
        current = by_id.get(entry_id)
        if id_counts[entry_id] > 1:
            reason = "edits内でidが重複しています"
        elif current is None:
            reason = "存在しないidです"
        elif current["status"] == "locked":
            reason = "lockedは変更できません"
        elif not check_placeholders(current["src"], tgt, patterns, tgt_only_patterns):
            reason = "プレースホルダーが原文と一致しません"
        else:
            if current["tgt"] and current["tgt"] != tgt:
                current["prev_tgt"] = current["tgt"]
            current["tgt"] = tgt
            current["status"] = status
            if "note" in edit:
                current["note"] = edit["note"]
            applied.append(entry_id)
            continue
        if (entry_id, reason) not in rejected:
            rejected.append((entry_id, reason))

    return applied, rejected


def _unique_tgts(rows: list[dict], status: str) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for r in rows:
        # tgt == src の行（原語のまま確定した行など）は流用・統一の基準にしない
        if r["status"] == status and r["tgt"] and r["tgt"] != r["src"]:
            index.setdefault(r["src"], set()).add(r["tgt"])
    return index


def prefill(rows: list[dict], patterns: list[str], tgt_only_patterns: list[str] = ()) -> list[str]:
    """モデルを呼ばずに埋まる untranslated/stale 行を埋め、埋めたidを返す。rowsはその場で更新する。

    (a) 訳す語が無い（プレースホルダーと記号だけ）→ tgt = src
    (b) locked に同一srcの訳が1種類だけある → その訳
    (c) (b)が無く、translated に同一srcの訳が1種類だけある → その訳
    (b)(c) はプレースホルダー構成が一致するときだけ。locked に複数訳がある src は
    意図的な訳し分けなので決め打ちしない。どれも status は translated（QAの対象に残す）。
    rows は全ファイル分を渡すこと（別ファイルにしか無い同一srcを拾うため）。
    """
    locked = _unique_tgts(rows, "locked")
    translated = _unique_tgts(rows, "translated")
    filled: list[str] = []

    for r in rows:
        if r["status"] not in PREFILLABLE_STATUSES:
            continue
        if not has_translatable_text(r["src"], patterns):
            tgt = r["src"]
        else:
            candidates = locked.get(r["src"]) or translated.get(r["src"])
            if not candidates or len(candidates) != 1:
                continue
            tgt = next(iter(candidates))
            if not check_placeholders(r["src"], tgt, patterns, tgt_only_patterns):
                continue
        if r["tgt"] and r["tgt"] != tgt:
            r["prev_tgt"] = r["tgt"]
        r["tgt"], r["status"] = tgt, "translated"
        filled.append(r["id"])

    return filled


_NUM = re.compile(r"\d+")


def id_sort_key(entry_id: str) -> tuple[tuple[int, ...], str]:
    """id中の数値列で比較するキー（`Map2` < `Map10`）。

    数値列の個数はidごとに違うので、平坦化せずタプルにネストして int と str の比較を避ける。
    """
    return (tuple(int(n) for n in _NUM.findall(entry_id)), entry_id)


def unify(
    rows: list[dict],
    skip_srcs: set[str] = frozenset(),
    dominance_threshold: float = DEFAULT_DOMINANCE_THRESHOLD,
) -> tuple[dict[str, str], list[str]]:
    """同一srcに複数の訳がある箇所を一本化する変更 {id: 新tgt} と、据え置いたsrcを返す。

    正とする訳: locked の最頻訳（locked が複数訳に割れ、最頻訳の占有率が
    dominance_threshold 未満なら原作の意図的な訳し分けとみなして据え置く）。
    locked に訳が無ければ、書き換え対象行の過半数の訳、過半数が無ければid順で最初の行の訳。
    書き換えるのは UNIFIABLE_STATUSES の行だけ。rows は変更しない。
    """
    by_src: dict[str, list[dict]] = {}
    for r in rows:
        if r["tgt"] and r["src"] not in skip_srcs:
            by_src.setdefault(r["src"], []).append(r)

    changes: dict[str, str] = {}
    left: list[str] = []
    for src, group in by_src.items():
        targets = [r for r in group if r["status"] in UNIFIABLE_STATUSES]
        if not targets or len({r["tgt"] for r in group}) <= 1:
            continue
        locked = Counter(r["tgt"] for r in group if r["status"] == "locked" and r["tgt"] != src)
        if locked:
            canon, n = locked.most_common(1)[0]
            if n / sum(locked.values()) < dominance_threshold:
                left.append(src)
                continue
        else:
            canon, n = Counter(r["tgt"] for r in targets).most_common(1)[0]
            if n * 2 <= len(targets):
                canon = min(targets, key=lambda r: id_sort_key(r["id"]))["tgt"]
        for r in targets:
            if r["tgt"] != canon:
                changes[r["id"]] = canon

    return changes, left


def count_statuses(rows_by_file: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    """ファイルごとの status 件数を返す。"""
    return {name: dict(Counter(r["status"] for r in rows)) for name, rows in rows_by_file.items()}


def render_unify_report(changes: dict[str, str], left: list[str]) -> str:
    lines = ["# 訳ゆれ統一レポート", "", f"変更行数: {len(changes)}", ""]
    if changes:
        lines.append("## 変更するid（新しい訳）")
        lines += [f"- [{i}] {t[:60]!r}" for i, t in sorted(changes.items())]
        lines.append("")
    if left:
        lines.append("## lockedの訳が割れているため据え置いたsrc（意図的な訳し分けの可能性）")
        lines += [f"- {s[:80]!r}" for s in sorted(left)]
        lines.append("")
    if not changes and not left:
        lines.append("訳ゆれは見つかりませんでした。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="翻訳後の修正ツール")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("project")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("project")
    p_apply.add_argument("edits", help='[{"id": ..., "tgt": ..., "note": ...}] のJSONファイル')
    p_apply.add_argument("--status", default="translated", choices=["translated", "reviewed"])
    p_apply.add_argument("--dry-run", action="store_true")
    p_prefill = sub.add_parser("prefill")
    p_prefill.add_argument("project")
    p_prefill.add_argument("--dry-run", action="store_true")
    p_unify = sub.add_parser("unify")
    p_unify.add_argument("project")
    p_unify.add_argument("--fix", action="store_true", help="統一した訳を書き戻す")
    args = parser.parse_args(argv)

    project = Path(args.project)
    cfg = json.loads((project / "tl.config.json").read_text(encoding="utf-8"))
    patterns = cfg["placeholder_patterns"]
    tgt_only = cfg.get("tgt_only_patterns", [])
    paths = sorted((project / "entries").glob("*.jsonl"))
    rows_by_path = {p: entries_mod.load_jsonl(p) for p in paths}
    # ponytail: id はプロジェクト全体で一意という前提（ファイル跨ぎの重複idは片方しか書き換わらない）
    all_rows = [r for rows in rows_by_path.values() for r in rows]
    before = {p: json.dumps(rows, ensure_ascii=False) for p, rows in rows_by_path.items()}
    write = False

    if args.cmd == "status":
        counts = count_statuses({p.name: rows for p, rows in rows_by_path.items()})
        for name, c in counts.items():
            print(f"{name}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
        total = count_statuses({"合計": all_rows})["合計"]
        print("合計: " + ", ".join(f"{k}={v}" for k, v in sorted(total.items())))
        pending = {k: total[k] for k in ("untranslated", "stale", "needs-review") if k in total}
        if pending:
            print(f"注意: 書き戻すと原語・旧訳・未修正の訳が出る行が残っています: {pending}")
    elif args.cmd == "apply":
        edits = json.loads(Path(args.edits).read_text(encoding="utf-8"))
        applied, rejected = apply_edits(all_rows, edits, patterns, tgt_only, args.status)
        print(f"書き込み: {len(applied)}件, 拒否: {len(rejected)}件")
        for entry_id, reason in rejected:
            print(f"  拒否 [{entry_id}] {reason}")
        write = not args.dry_run
    elif args.cmd == "prefill":
        filled = prefill(all_rows, patterns, tgt_only)
        print(f"prefill: {len(filled)}件")
        write = not args.dry_run
    elif args.cmd == "unify":
        changes, left = unify(
            all_rows,
            set(cfg.get("unify_skip_srcs", [])),
            cfg.get("unify_dominance_threshold", DEFAULT_DOMINANCE_THRESHOLD),
        )
        report = project / "qa" / "unify-report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_unify_report(changes, left), encoding="utf-8")
        print(f"訳ゆれ: 変更{len(changes)}行, 据え置き{len(left)}件 -> {report}")
        if args.fix:
            for r in all_rows:
                if r["id"] in changes:
                    r["prev_tgt"], r["tgt"] = r["tgt"], changes[r["id"]]
            write = True

    if args.cmd != "status" and not write:
        print("（dry-run: 書き込みませんでした）" if getattr(args, "dry_run", False) else "（書き込みませんでした。--fix で反映）")
    if write:
        for p, rows in rows_by_path.items():
            if json.dumps(rows, ensure_ascii=False) != before[p]:
                entries_mod.save_jsonl(p, rows)


if __name__ == "__main__":
    main()
