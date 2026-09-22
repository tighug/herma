"""translationese.py のテスト。原作訳（locked）を物差しにした翻訳調の測定を合成データで検証する。"""
import json

from scripts import entries, translationese as te

CFG = te.Config({"placeholder_patterns": [r"\\[A-Za-z]+\[[^\]]*\]"]})


def row(id_, src, tgt, status):
    return {"id": id_, "src": src, "tgt": tgt, "ctx": "", "status": status,
            "hash": entries.hash_of(src), "prev_tgt": None, "note": ""}


def she_rows(n_locked_hit, n_locked, n_mt_hit, n_mt, mt_status="translated"):
    """she を含む原文で、「彼女」と訳した行を指定数だけ作る。"""
    rows = []
    for i in range(n_locked):
        rows.append(row(f"l{i}", "She smiled.", "彼女は笑った" if i < n_locked_hit else "笑った", "locked"))
    for i in range(n_mt):
        rows.append(row(f"t{i}", "She smiled.", "彼女は微笑んだ" if i < n_mt_hit else "微笑んだ", mt_status))
    return rows


# --- 率の比較 ---

def test_rates_count_only_rows_whose_source_has_the_word():
    rows = she_rows(1, 20, 10, 40) + [row("x", "It rains.", "雨が降る", "translated")]

    r = te.calque_rates(rows, CFG)["彼女"]

    assert (r["n_l"], r["n_t"]) == (20, 40)  # she の無い行は母数に入らない
    assert r["l"] == 0.05 and r["t"] == 0.25


def test_reviewed_and_needs_review_rows_are_measured_but_stale_and_untranslated_are_not():
    rows = (
        she_rows(0, 0, 30, 30, "reviewed")
        + [row(f"s{i}", "She smiled.", "彼女は笑った", "stale") for i in range(5)]
        + [row(f"n{i}", "She smiled.", "彼女は笑った", "needs-review") for i in range(5)]
        + [row("u", "She smiled.", "", "untranslated")]
    )

    assert te.calque_rates(rows, CFG)["彼女"]["n_t"] == 35


def test_adopted_requires_ratio_gap_and_sample_size():
    adopted = lambda rows: "彼女" in te.adopted(te.calque_rates(rows, CFG), CFG)  # noqa: E731

    assert adopted(she_rows(2, 40, 10, 40))          # 5% → 25%
    assert not adopted(she_rows(2, 40, 10, 29))      # 機械翻訳の母数不足
    assert not adopted(she_rows(0, 11, 30, 40))      # 原作の母数不足（0%/11行を物差しにしない）
    assert not adopted(she_rows(10, 20, 24, 40))     # 50% → 60%: 2倍に届かない
    assert not adopted(she_rows(1, 100, 3, 100))     # 1% → 3%: 3倍だが差が5ptに届かない


def test_undecided_keeps_large_gaps_whose_original_sample_is_too_small():
    """原作 0%（11行）を物差しに採用はしないが、67%との差は捨てずに要判断として出す。"""
    rows = she_rows(0, 11, 30, 40)

    result = te.measure(rows, CFG)

    assert result["adopted"] == [] and result["undecided"] == ["彼女"]
    assert result["tags"] == {}  # 要判断の語ではタグを付けない
    assert result["exemplars"]["彼女"]


def test_report_marks_rates_with_a_small_original_sample_as_reference_only():
    result = te.measure(she_rows(6, 12, 30, 40), CFG)  # 原作 50%（12行）を物差しにしない

    report = te.render_report(result, CFG)

    assert "| 彼女 |  | 75.0% | 40 | 50.0%（参考） | 12 |" in report


def test_kanji_only_translations_count_as_prose():
    rows = [row("t", "Really?", "本当？", "translated")]

    assert te.calque_rates(rows, CFG)["本当に"] == {"n_t": 1, "t": 1.0, "n_l": 0, "l": 0.0}


def test_avoided_lists_words_the_original_translation_rarely_renders_even_without_mt_rows():
    """翻訳前（機械翻訳の行が無い）でも、原作が訳さずに済ませている語を出せる。"""
    rows = she_rows(2, 40, 0, 0)

    result = te.measure(rows, CFG)

    assert result["adopted"] == []
    assert "彼女" in result["avoided"]
    assert result["exemplars"]["彼女"][0] == {"src": "She smiled.", "tgt": "笑った"}


# --- タグ付け ---

def test_flag_rows_tags_only_mt_rows_that_use_an_adopted_word():
    rows = she_rows(1, 20, 10, 40)

    tags = te.flag_rows(rows, ["彼女"], CFG)

    assert set(tags) == {f"t{i}" for i in range(10)}
    assert tags["t0"] == ["calque:彼女"]


def test_flag_rows_skips_rows_identical_to_a_locked_pair_and_excluded_ids():
    """原作と同じ (src, tgt) は原作の言い回しそのもの。直すと原作と食い違う。"""
    rows = [
        row("l", "She smiled.", "彼女は笑った", "locked"),
        row("t1", "She smiled.", "彼女は笑った", "translated"),
        row("t2", "She laughed.", "彼女は笑い転げた", "translated"),
        row("t3", "She cried.", "彼女は泣いた", "translated"),
    ]

    tags = te.flag_rows(rows, ["彼女"], CFG, exclude_ids={"t3"})

    assert set(tags) == {"t2"}


def test_non_prose_prefixes_are_excluded_from_rates_and_tags():
    cfg = te.Config({"translationese": {"non_prose_id_prefixes": ["Items/"]}})
    rows = [row("Items/1/description", "She wears it.", "彼女が着る服", "translated")]

    assert te.calque_rates(rows, cfg)["彼女"]["n_t"] == 0
    assert te.flag_rows(rows, ["彼女"], cfg) == {}


def test_extra_calques_from_config_are_measured():
    cfg = te.Config({"translationese": {"extra_calques": [["クソ", r"\bdamn\b", r"[くク][そソ]"]]}})
    rows = [row("t", "Damn it.", "クソッ", "translated")]

    assert te.calque_rates(rows, cfg)["クソ"] == {"n_t": 1, "t": 1.0, "n_l": 0, "l": 0.0}


# --- 地の文の組み立て ---

def test_narration_tags_comma_chains_and_progressive_calques_ignoring_placeholders():
    rows = [
        row("a", "He runs, fast, hard.", "\\c[27]男は夜の街を走り、速く、どこまでも激しく突き進む", "translated"),
        row("b", "It is throbbing and pulsing.", "脈打っている肉が小刻みに震えていく感触が指先まで伝わる", "translated"),
        row("c", "He waits.", "男は黙ったまま長い間ずっと扉の前で待ち続けた", "translated"),
    ]
    narration = te.default_narration_ids(rows, CFG)

    tags = te.flag_rows(rows, [], CFG, narration)

    assert narration == {"a", "b", "c"}
    assert tags == {"a": ["comma"], "b": ["teiru"]}


def test_default_narration_ids_skip_quoted_speech_and_short_lines():
    rows = [
        row("q", "Guard: Halt, you, now.", "衛兵「止まれ、お前、今すぐにだ、そこを動くな」", "translated"),
        row("s", "Run.", "走る", "translated"),
    ]

    assert te.default_narration_ids(rows, CFG) == set()


# --- CLI ---

def test_main_writes_reports_and_never_changes_entries(tmp_path):
    (tmp_path / "entries").mkdir()
    (tmp_path / "tl.config.json").write_text(json.dumps({"placeholder_patterns": []}), encoding="utf-8")
    path = tmp_path / "entries" / "a.jsonl"
    entries.save_jsonl(path, she_rows(2, 40, 10, 40))
    before = path.read_bytes()

    result = te.main([str(tmp_path)])

    assert path.read_bytes() == before  # status を降格しない
    assert result["adopted"] == ["彼女"]
    saved = json.loads((tmp_path / "qa" / "translationese.json").read_text(encoding="utf-8"))
    assert len(saved["tags"]) == 10
    report = (tmp_path / "qa" / "translationese-report.md").read_text(encoding="utf-8")
    assert "| 彼女 | 採用・原作が避ける |" in report
