"""fix.py のテスト。id指定の書き込み・prefill・訳ゆれ統一・status集計を検証する。"""
import json

from scripts import entries, fix

PATTERNS = [r"\{[^}]*\}", r"<[^>]+>"]


def row(id_, src, tgt="", status="untranslated", **extra):
    return {"id": id_, "src": src, "tgt": tgt, "ctx": "", "status": status,
            "hash": entries.hash_of(src), "prev_tgt": None, "note": "", **extra}


# --- apply_edits ---

def test_apply_edits_writes_tgt_and_status_and_keeps_old_tgt_in_prev_tgt():
    rows = [row("a", "Hello", "やあ", "translated")]

    applied, rejected = fix.apply_edits(
        rows, [{"id": "a", "tgt": "こんにちは", "note": "直した"}], PATTERNS, status="reviewed"
    )

    assert applied == ["a"] and rejected == []
    assert rows[0]["tgt"] == "こんにちは"
    assert rows[0]["prev_tgt"] == "やあ"
    assert rows[0]["status"] == "reviewed"
    assert rows[0]["note"] == "直した"


def test_apply_edits_does_not_overwrite_prev_tgt_with_empty_tgt():
    rows = [row("a", "Hello")]

    fix.apply_edits(rows, [{"id": "a", "tgt": "こんにちは"}], PATTERNS)

    assert rows[0]["prev_tgt"] is None
    assert rows[0]["status"] == "translated"


def test_apply_edits_rejects_unknown_locked_placeholder_mismatch_and_duplicate_ids():
    rows = [
        row("locked", "Hi", "やあ", "locked"),
        row("ph", "{name} Hello"),
        row("dup", "Bye"),
    ]
    edits = [
        {"id": "ghost", "tgt": "x"},
        {"id": "locked", "tgt": "こんちは"},
        {"id": "ph", "tgt": "こんにちは"},
        {"id": "dup", "tgt": "さよなら"},
        {"id": "dup", "tgt": "またね"},
    ]

    applied, rejected = fix.apply_edits(rows, edits, PATTERNS)

    assert applied == []
    assert {id_ for id_, _ in rejected} == {"ghost", "locked", "ph", "dup"}
    assert rows[0]["tgt"] == "やあ"
    assert rows[1]["tgt"] == "" and rows[2]["tgt"] == ""


def test_apply_edits_accepts_tgt_only_tags():
    rows = [row("a", "Hello")]

    applied, _ = fix.apply_edits(
        rows, [{"id": "a", "tgt": "<ruby>今日</ruby>は"}], [r"<[^>]+>"],
        tgt_only_patterns=[r"</?ruby>"],
    )

    assert applied == ["a"]


# --- prefill ---

def test_prefill_fills_placeholder_only_row_with_src():
    rows = [row("a", "{name}...!")]

    filled = fix.prefill(rows, PATTERNS)

    assert filled == ["a"]
    assert rows[0]["tgt"] == "{name}...!"
    assert rows[0]["status"] == "translated"


def test_prefill_reuses_unique_locked_translation_before_translated_one():
    rows = [
        row("l", "Hello", "こんにちは", "locked"),
        row("t", "Hello", "ハロー", "translated"),
        row("a", "Hello"),
    ]

    fix.prefill(rows, PATTERNS)

    assert rows[2]["tgt"] == "こんにちは"
    assert rows[2]["status"] == "translated"


def test_prefill_reuses_unique_translated_translation_when_no_locked():
    rows = [row("t", "Hello", "こんにちは", "translated"), row("a", "Hello", status="stale")]

    fix.prefill(rows, PATTERNS)

    assert rows[1]["tgt"] == "こんにちは"


def test_prefill_skips_ambiguous_or_placeholder_mismatched_candidates():
    rows = [
        row("l1", "Leave", "やめる", "locked"),
        row("l2", "Leave", "立ち去る", "locked"),
        row("a", "Leave"),
        row("t", "{n} coins", "コイン", "translated"),  # 壊れた既訳（プレースホルダー欠落）
        row("b", "{n} coins"),
    ]

    filled = fix.prefill(rows, PATTERNS)

    assert filled == []
    assert rows[2]["tgt"] == "" and rows[4]["tgt"] == ""


def test_prefill_ignores_rows_that_are_not_waiting_for_translation():
    rows = [row("t", "Hello", "こんにちは", "translated"), row("r", "Hello", "やあ", "reviewed")]

    assert fix.prefill(rows, PATTERNS) == []
    assert rows[1]["tgt"] == "やあ"


# --- unify ---

def test_unify_prefers_locked_translation():
    rows = [
        row("l", "Shop", "店", "locked"),
        row("a", "Shop", "ショップ", "translated"),
        row("b", "Shop", "ショップ", "needs-review"),
    ]

    changes, left = fix.unify(rows)

    assert changes == {"a": "店", "b": "店"} and left == []


def test_unify_leaves_group_when_locked_translations_are_split():
    rows = [
        row("l1", "Merchant", "貿易商", "locked"),
        row("l2", "Merchant", "商人", "locked"),
        row("a", "Merchant", "商売人", "translated"),
    ]

    changes, left = fix.unify(rows, dominance_threshold=0.9)

    assert changes == {} and left == ["Merchant"]


def test_unify_uses_majority_then_first_id_without_locked():
    majority = [
        row("m1", "Hi", "やあ", "translated"),
        row("m2", "Hi", "やあ", "translated"),
        row("m3", "Hi", "よう", "translated"),
    ]
    tie = [
        row("Map010/1", "Bye", "またね", "translated"),
        row("Map002/1", "Bye", "さよなら", "translated"),
    ]

    changes, _ = fix.unify(majority + tie)

    assert changes == {"m3": "やあ", "Map010/1": "さよなら"}


def test_unify_never_touches_reviewed_stale_or_skipped_srcs():
    rows = [
        row("l", "Shop", "店", "locked"),
        row("r", "Shop", "ショップ", "reviewed"),
        row("s", "Shop", "ショップ", "stale"),
        row("x1", "Hnngh", "んっ", "translated"),
        row("x2", "Hnngh", "んぐっ", "translated"),
    ]

    changes, _ = fix.unify(rows, skip_srcs={"Hnngh"})

    assert changes == {}


# --- CLI（ファイルI/O） ---

def _project(tmp_path, files: dict[str, list[dict]], cfg: dict | None = None):
    (tmp_path / "entries").mkdir()
    for name, rows in files.items():
        entries.save_jsonl(tmp_path / "entries" / name, rows)
    (tmp_path / "tl.config.json").write_text(
        json.dumps({"placeholder_patterns": PATTERNS, **(cfg or {})}), encoding="utf-8"
    )
    return tmp_path


def test_cli_apply_dry_run_does_not_write(tmp_path):
    project = _project(tmp_path, {"a.jsonl": [row("a", "Hello")]})
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps([{"id": "a", "tgt": "こんにちは"}]), encoding="utf-8")

    fix.main(["apply", str(project), str(edits), "--dry-run"])
    assert entries.load_jsonl(project / "entries" / "a.jsonl")[0]["tgt"] == ""

    fix.main(["apply", str(project), str(edits)])
    assert entries.load_jsonl(project / "entries" / "a.jsonl")[0]["tgt"] == "こんにちは"


def test_cli_prefill_reuses_translation_across_files_and_saves_only_changed(tmp_path):
    project = _project(tmp_path, {
        "a.jsonl": [row("l", "Hello", "こんにちは", "locked")],
        "b.jsonl": [row("b", "Hello")],
    })
    before = (project / "entries" / "a.jsonl").stat().st_mtime_ns

    fix.main(["prefill", str(project)])

    assert entries.load_jsonl(project / "entries" / "b.jsonl")[0]["tgt"] == "こんにちは"
    assert (project / "entries" / "a.jsonl").stat().st_mtime_ns == before


def test_cli_unify_reports_by_default_and_writes_with_fix(tmp_path):
    project = _project(
        tmp_path,
        {"a.jsonl": [row("l", "Shop", "店", "locked"), row("a", "Shop", "ショップ", "translated")]},
    )

    fix.main(["unify", str(project)])
    assert entries.load_jsonl(project / "entries" / "a.jsonl")[1]["tgt"] == "ショップ"
    assert "[a]" in (project / "qa" / "unify-report.md").read_text(encoding="utf-8")

    fix.main(["unify", str(project), "--fix"])
    assert entries.load_jsonl(project / "entries" / "a.jsonl")[1]["tgt"] == "店"


def test_count_statuses_per_file():
    counts = fix.count_statuses({"a.jsonl": [row("a", "x"), row("b", "y", "z", "stale")]})

    assert counts == {"a.jsonl": {"untranslated": 1, "stale": 1}}
