"""entries.py のテスト。JSONL入出力・hash計算・status遷移を検証する。"""
from scripts import entries


def test_hash_of_returns_stable_hash_for_same_text():
    assert entries.hash_of("Hello, {playerName}!") == entries.hash_of("Hello, {playerName}!")


def test_hash_of_differs_for_different_text():
    assert entries.hash_of("Hello") != entries.hash_of("Goodbye")


def test_load_jsonl_reads_entries_from_file(tmp_path):
    path = tmp_path / "entries.jsonl"
    path.write_text(
        '{"id":"a","src":"Hello","tgt":"","ctx":"","status":"untranslated","hash":"h1","prev_tgt":null,"note":""}\n'
        '{"id":"b","src":"World","tgt":"世界","ctx":"","status":"translated","hash":"h2","prev_tgt":null,"note":""}\n',
        encoding="utf-8",
    )

    loaded = entries.load_jsonl(path)

    assert [e["id"] for e in loaded] == ["a", "b"]
    assert loaded[1]["tgt"] == "世界"


def test_load_jsonl_returns_empty_list_for_missing_file(tmp_path):
    path = tmp_path / "does-not-exist.jsonl"

    assert entries.load_jsonl(path) == []


def test_save_jsonl_writes_one_json_object_per_line(tmp_path):
    path = tmp_path / "out.jsonl"
    data = [
        {"id": "a", "src": "Hello", "tgt": ""},
        {"id": "b", "src": "World", "tgt": "世界"},
    ]

    entries.save_jsonl(path, data)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"id": "a"' not in lines[0]  # compact separators, not pretty-printed
    assert "世界" in lines[1]  # ensure_ascii=False で日本語がエスケープされない


def test_merge_extracted_adds_new_entry_as_untranslated():
    existing: list[dict] = []
    extracted = [{"id": "a", "src": "Hello", "ctx": "greeting"}]

    merged = entries.merge_extracted(existing, extracted)

    assert merged == [
        {
            "id": "a",
            "src": "Hello",
            "tgt": "",
            "ctx": "greeting",
            "status": "untranslated",
            "hash": entries.hash_of("Hello"),
            "prev_tgt": None,
            "note": "",
        }
    ]


def test_merge_extracted_keeps_entry_untouched_when_hash_unchanged():
    existing = [
        {
            "id": "a",
            "src": "Hello",
            "tgt": "こんにちは",
            "ctx": "greeting",
            "status": "translated",
            "hash": entries.hash_of("Hello"),
            "prev_tgt": None,
            "note": "memo",
        }
    ]
    extracted = [{"id": "a", "src": "Hello", "ctx": "greeting"}]

    merged = entries.merge_extracted(existing, extracted)

    assert merged == existing


def test_merge_extracted_marks_stale_and_preserves_old_translation_when_src_changes():
    existing = [
        {
            "id": "a",
            "src": "Hello",
            "tgt": "こんにちは",
            "ctx": "greeting",
            "status": "translated",
            "hash": entries.hash_of("Hello"),
            "prev_tgt": None,
            "note": "",
        }
    ]
    extracted = [{"id": "a", "src": "Hello there", "ctx": "greeting"}]

    merged = entries.merge_extracted(existing, extracted)

    assert merged == [
        {
            "id": "a",
            "src": "Hello there",
            "tgt": "こんにちは",
            "ctx": "greeting",
            "status": "stale",
            "hash": entries.hash_of("Hello there"),
            "prev_tgt": "こんにちは",
            "note": "",
        }
    ]


def test_merge_extracted_leaves_locked_entry_completely_unchanged():
    existing = [
        {
            "id": "a",
            "src": "Hello",
            "tgt": "こんにちは",
            "ctx": "greeting",
            "status": "locked",
            "hash": entries.hash_of("Hello"),
            "prev_tgt": None,
            "note": "",
        }
    ]
    extracted = [{"id": "a", "src": "Hello there", "ctx": "greeting"}]

    merged = entries.merge_extracted(existing, extracted)

    assert merged == existing
