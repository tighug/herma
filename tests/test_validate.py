"""validate.py のテスト。プレースホルダー保持・未訳・用語集・不統一・長さ超過を検証する。"""
from scripts import validate

PATTERNS = [r"\{[^}]*\}", r"%[sd]", r"<[^>]+>", r"\\n"]


def test_extract_tokens_finds_all_placeholder_matches():
    tokens = validate.extract_tokens("Hi {name}, you have %d <color=#fff>items</color>", PATTERNS)
    assert tokens == ["{name}", "%d", "<color=#fff>", "</color>"]


def test_check_placeholders_passes_when_tokens_match_as_multiset():
    src = "Hi {name}, welcome!"
    tgt = "こんにちは{name}さん！"
    assert validate.check_placeholders(src, tgt, PATTERNS) is True


def test_check_placeholders_fails_when_a_token_is_dropped():
    src = "Hi {name}, you have {count} items"
    tgt = "こんにちは{name}さん"  # {count} が欠落

    assert validate.check_placeholders(src, tgt, PATTERNS) is False


def test_check_placeholders_fails_when_token_count_differs():
    src = "line1\\nline2"
    tgt = "line1"  # \n が欠落

    assert validate.check_placeholders(src, tgt, PATTERNS) is False


def test_is_untranslated_true_when_tgt_is_empty():
    assert validate.is_untranslated({"src": "Hello", "tgt": ""}) is True


def test_is_untranslated_true_when_tgt_equals_src():
    assert validate.is_untranslated({"src": "Hello", "tgt": "Hello"}) is True


def test_is_untranslated_false_when_translated():
    assert validate.is_untranslated({"src": "Hello", "tgt": "こんにちは"}) is False


def test_check_glossary_flags_missing_translation_of_known_term():
    glossary = {"Sword of Dawn": "暁の剣"}
    src = "You found the Sword of Dawn."
    tgt = "何かを見つけた。"  # 用語集の訳語が入っていない

    assert validate.check_glossary(src, tgt, glossary) is False


def test_check_glossary_passes_when_translation_present():
    glossary = {"Sword of Dawn": "暁の剣"}
    src = "You found the Sword of Dawn."
    tgt = "暁の剣を見つけた。"

    assert validate.check_glossary(src, tgt, glossary) is True


def test_check_glossary_passes_when_term_not_in_src():
    glossary = {"Sword of Dawn": "暁の剣"}
    assert validate.check_glossary("Hello there", "こんにちは", glossary) is True


def test_find_inconsistent_translations_detects_same_src_different_tgt():
    rows = [
        {"id": "a1", "src": "Attack", "tgt": "攻撃"},
        {"id": "a2", "src": "Attack", "tgt": "こうげき"},
        {"id": "a3", "src": "Defend", "tgt": "防御"},
    ]

    result = validate.find_inconsistent_translations(rows)

    assert result == {"Attack": ["攻撃", "こうげき"]}


def test_find_inconsistent_translations_ignores_untranslated_rows():
    # 同じsrcの片方がまだ未訳(tgt=="")なだけで、不統一として誤検知してはならない
    rows = [
        {"id": "a1", "src": "Attack", "tgt": "攻撃"},
        {"id": "a2", "src": "Attack", "tgt": ""},
    ]

    result = validate.find_inconsistent_translations(rows)

    assert result == {}


def test_check_length_ratio_fails_when_tgt_too_long():
    assert validate.check_length_ratio("Hi", "こんにちはこんにちは", max_ratio=2.0) is False


def test_check_length_ratio_passes_when_ratio_disabled():
    assert validate.check_length_ratio("Hi", "こんにちはこんにちはこんにちは", max_ratio=None) is True


def test_run_validation_flags_placeholder_violation_as_needs_review():
    rows = [
        {
            "id": "a",
            "src": "Hi {name}",
            "tgt": "こんにちは",  # {name} 欠落
            "status": "translated",
        }
    ]

    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert report.violations[0].entry_id == "a"
    assert report.violations[0].kind == "placeholder"
    assert rows[0]["status"] == "needs-review"


def test_run_validation_leaves_clean_entry_status_untouched():
    rows = [
        {
            "id": "a",
            "src": "Hi {name}",
            "tgt": "こんにちは{name}さん",
            "status": "translated",
        }
    ]

    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert report.violations == []
    assert rows[0]["status"] == "translated"


def test_run_validation_skips_placeholder_check_for_untranslated_entries():
    rows = [{"id": "a", "src": "Hi {name}", "tgt": "", "status": "untranslated"}]

    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    kinds = [v.kind for v in report.violations]
    assert "placeholder" not in kinds
    assert "untranslated" in kinds
    # untranslatedは翻訳待ちの正常状態であり、QAで needs-review に落として
    # 翻訳対象(tl-translateのTRANSLATABLE_STATUSES)から外してはならない
    assert rows[0]["status"] == "untranslated"


def test_run_validation_does_not_demote_stale_entries_out_of_translation_queue():
    # staleはtgtが旧訳・srcが新原文なので、プレースホルダー等の機械チェックは
    # 誤検知しうる。それでも stale というステータス自体は保持し、
    # tl-translate の再翻訳対象から外してはならない
    rows = [
        {
            "id": "a",
            "src": "Hi there {name}",
            "tgt": "こんにちは",  # 旧原文"Hi"に対する旧訳。{name}が無くて当然
            "status": "stale",
        }
    ]

    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert rows[0]["status"] == "stale"


def test_run_validation_does_not_unfreeze_locked_entries():
    rows = [
        {
            "id": "a",
            "src": "Hi {name}",
            "tgt": "こんにちは",  # 意図的に{name}を訳文へ含めない固定訳
            "status": "locked",
        }
    ]

    validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert rows[0]["status"] == "locked"


def test_run_validation_excludes_locked_entries_from_report():
    # locked は「正しいと表明済み」の凍結エントリなので検証対象外。
    # 原文復元エントリのように src/tgt の言語が異なりプレースホルダーが
    # 構造上一致しない場合でも、違反として報告してはならない
    rows = [
        {"id": "a", "src": "Hi {name}", "tgt": "固定訳（プレースホルダー無し）", "status": "locked"},
    ]

    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert report.violations == []


def test_find_inconsistent_translations_ignores_disagreement_among_locked_only():
    # locked同士だけの「同一原文・別訳」は、大量の確定訳が存在するプロジェクトで
    # レポートを埋めるノイズになるため報告しない
    rows = [
        {"id": "a1", "src": "Attack", "tgt": "攻撃", "status": "locked"},
        {"id": "a2", "src": "Attack", "tgt": "こうげき", "status": "locked"},
    ]

    result = validate.find_inconsistent_translations(rows)

    assert result == {}


def test_find_inconsistent_translations_flags_locked_vs_non_locked_disagreement():
    # locked は ground truth なので、新規翻訳がlockedの確定訳と食い違う場合は
    # 引き続き検出する
    rows = [
        {"id": "a1", "src": "Attack", "tgt": "こうげき", "status": "locked"},
        {"id": "a2", "src": "Attack", "tgt": "攻撃", "status": "translated"},
    ]

    result = validate.find_inconsistent_translations(rows)

    assert result == {"Attack": ["こうげき", "攻撃"]}


def test_run_validation_detects_glossary_violation():
    rows = [
        {"id": "a", "src": "the Sword of Dawn", "tgt": "何か", "status": "translated"},
    ]
    glossary = {"Sword of Dawn": "暁の剣"}

    report = validate.run_validation(rows, patterns=PATTERNS, glossary=glossary, max_len_ratio=None)

    assert any(v.kind == "glossary" and v.entry_id == "a" for v in report.violations)


def test_run_validation_detects_inconsistent_translation():
    rows = [
        {"id": "a1", "src": "Attack", "tgt": "攻撃", "status": "translated"},
        {"id": "a2", "src": "Attack", "tgt": "こうげき", "status": "translated"},
    ]

    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    assert any(v.kind == "inconsistent" for v in report.violations)


def test_render_report_produces_markdown_with_violation_sections():
    rows = [{"id": "a", "src": "Hi {name}", "tgt": "こんにちは", "status": "translated"}]
    report = validate.run_validation(rows, patterns=PATTERNS, glossary={}, max_len_ratio=None)

    markdown = validate.render_report(report)

    assert "placeholder" in markdown
    assert "a" in markdown
