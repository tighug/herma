"""fixtures/fake_game のアダプタ例に対する往復テスト。

対象ゲームが毎回異なり実ゲームでの検証ができないため、架空ゲームで
中間層の動作（extract→translate→inject の非破壊性、プレースホルダー保持、
hash差分によるstale化）を保証する。
"""
import json
from pathlib import Path

from fixtures.fake_game import extract, inject
from scripts import entries, validate

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "fake_game" / "dialogue.json"
PATTERNS = [r"\{[^}]*\}", r"<[^>]+>", r"\\n"]


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_extract_flattens_nested_dict_into_id_src_ctx_rows():
    data = load_fixture()

    rows = extract.extract_from_dict(data)

    ids = {r["id"] for r in rows}
    assert ids == {"scenes/scene01/0001", "scenes/scene01/0002", "scenes/scene02/0001"}
    row = next(r for r in rows if r["id"] == "scenes/scene01/0001")
    assert row["src"] == "Hello, {playerName}!\nWelcome to the village."
    assert row["ctx"] == "scenes/scene01"


def test_roundtrip_reproduces_original_structure_before_translation():
    data = load_fixture()
    rows = extract.extract_from_dict(data)
    merged = entries.merge_extracted([], rows)

    rebuilt = inject.inject_into_dict(data, {e["id"]: e for e in merged})

    # 未翻訳（tgtが空）なら原文がそのまま書き戻され、元の構造と一致する
    assert rebuilt == data


def test_roundtrip_preserves_placeholders_after_translation():
    data = load_fixture()
    rows = extract.extract_from_dict(data)
    merged = entries.merge_extracted([], rows)
    for e in merged:
        if e["id"] == "scenes/scene01/0001":
            e["tgt"] = "こんにちは、{playerName}！\n村へようこそ。"
            e["status"] = "translated"
        elif e["id"] == "scenes/scene01/0002":
            e["tgt"] = "<color=#ffffff>Aボタンで続ける</color>"
            e["status"] = "translated"

    rebuilt = inject.inject_into_dict(data, {e["id"]: e for e in merged})

    assert rebuilt["scenes"]["scene01"]["0001"] == "こんにちは、{playerName}！\n村へようこそ。"
    for token in ("{playerName}", "\n"):
        assert token in rebuilt["scenes"]["scene01"]["0001"]
    assert "<color=#ffffff>" in rebuilt["scenes"]["scene01"]["0002"]
    assert "</color>" in rebuilt["scenes"]["scene01"]["0002"]


def test_reextraction_marks_only_changed_entry_stale_and_keeps_others_translated():
    data = load_fixture()
    rows = extract.extract_from_dict(data)
    merged = entries.merge_extracted([], rows)
    for e in merged:
        e["tgt"] = f"訳:{e['src']}"
        e["status"] = "translated"

    # 原文更新: scene01/0001 のみ変更
    updated = json.loads(json.dumps(data))
    updated["scenes"]["scene01"]["0001"] = "Hi, {playerName}!\nWelcome to the village."
    new_rows = extract.extract_from_dict(updated)

    remerged = entries.merge_extracted(merged, new_rows)
    by_id = {e["id"]: e for e in remerged}

    assert by_id["scenes/scene01/0001"]["status"] == "stale"
    assert by_id["scenes/scene01/0001"]["prev_tgt"] == f"訳:Hello, {{playerName}}!\nWelcome to the village."
    assert by_id["scenes/scene01/0002"]["status"] == "translated"
    assert by_id["scenes/scene02/0001"]["status"] == "translated"


def test_validate_detects_placeholder_dropped_by_a_bad_translation():
    data = load_fixture()
    rows = extract.extract_from_dict(data)
    merged = entries.merge_extracted([], rows)
    target = next(e for e in merged if e["id"] == "scenes/scene01/0001")
    target["tgt"] = "こんにちは！村へようこそ。"  # {playerName} を落とした訳
    target["status"] = "translated"

    report = validate.run_validation(merged, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert any(
        v.kind == "placeholder" and v.entry_id == "scenes/scene01/0001"
        for v in report.violations
    )
