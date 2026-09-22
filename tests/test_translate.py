"""translate.py のテスト。バッチ翻訳対象の選定・チャンク化・id照合による書き戻しを検証する。"""
from scripts import translate


def test_select_translatable_includes_untranslated_and_stale():
    rows = [
        {"id": "a", "status": "untranslated"},
        {"id": "b", "status": "stale"},
        {"id": "c", "status": "translated"},
        {"id": "d", "status": "locked"},
        {"id": "e", "status": "needs-review"},
    ]

    selected = translate.select_translatable(rows)

    assert [r["id"] for r in selected] == ["a", "b"]


def test_chunk_entries_splits_into_groups_of_given_size():
    rows = [{"id": str(i), "src": f"src{i}"} for i in range(5)]

    chunks = translate.chunk_entries(rows, chunk_size=2)

    assert [[r["id"] for r in c] for c in chunks] == [["0", "1"], ["2", "3"], ["4"]]


def test_chunk_entries_keeps_identical_src_in_the_same_chunk():
    # 同一srcが別チャンクに散ると、モデルが別々に訳して訳ゆれが発生する
    rows = [
        {"id": "a1", "src": "Attack"},
        {"id": "b1", "src": "Defend"},
        {"id": "a2", "src": "Attack"},
        {"id": "c1", "src": "Heal"},
    ]

    chunks = translate.chunk_entries(rows, chunk_size=2)

    ids_by_chunk = [{r["id"] for r in c} for c in chunks]
    assert {"a1", "a2"}.issubset(next(c for c in ids_by_chunk if "a1" in c))


def test_chunk_entries_splits_a_group_larger_than_chunk_size_on_its_own():
    # 1つのsrcだけでchunk_sizeを超える場合は、そのグループ内で分割する
    # （1リクエストのmax_tokensを超えて丸ごと失敗しないように）
    rows = [{"id": f"a{i}", "src": "Attack"} for i in range(5)]

    chunks = translate.chunk_entries(rows, chunk_size=2)

    assert [[r["id"] for r in c] for c in chunks] == [
        ["a0", "a1"],
        ["a2", "a3"],
        ["a4"],
    ]


def test_apply_results_writes_back_matching_ids_and_marks_translated():
    rows = [
        {"id": "a", "src": "Hello", "tgt": "", "status": "untranslated", "prev_tgt": None},
        {"id": "b", "src": "World", "tgt": "", "status": "untranslated", "prev_tgt": None},
    ]
    sent_ids = {"a", "b"}
    results = [{"id": "a", "tgt": "こんにちは"}, {"id": "b", "tgt": "世界"}]

    outcome = translate.apply_results(rows, sent_ids, results)

    assert rows[0]["tgt"] == "こんにちは"
    assert rows[0]["status"] == "translated"
    assert rows[1]["tgt"] == "世界"
    assert outcome.applied == ["a", "b"]
    assert outcome.rejected_unknown_ids == []
    assert outcome.missing_ids == []


def test_apply_results_rejects_id_that_was_not_sent_in_this_chunk():
    rows = [
        {"id": "a", "src": "Hello", "tgt": "", "status": "untranslated", "prev_tgt": None},
    ]
    sent_ids = {"a"}
    # "z" はこのチャンクが送っていないid（他チャンク宛や不正な応答を想定）
    results = [{"id": "a", "tgt": "こんにちは"}, {"id": "z", "tgt": "何か"}]

    outcome = translate.apply_results(rows, sent_ids, results)

    assert rows[0]["tgt"] == "こんにちは"
    assert outcome.rejected_unknown_ids == ["z"]
    # "z" 用のエントリはrowsに存在しないため、どこにも書き込まれていないこと
    assert all(r["id"] != "z" for r in rows)


def test_apply_results_leaves_entry_untranslated_when_id_missing_from_response():
    rows = [
        {"id": "a", "src": "Hello", "tgt": "", "status": "untranslated", "prev_tgt": None},
        {"id": "b", "src": "World", "tgt": "", "status": "untranslated", "prev_tgt": None},
    ]
    sent_ids = {"a", "b"}
    results = [{"id": "a", "tgt": "こんにちは"}]  # "b" の応答が欠落

    outcome = translate.apply_results(rows, sent_ids, results)

    assert rows[1]["status"] == "untranslated"
    assert rows[1]["tgt"] == ""
    assert outcome.missing_ids == ["b"]


def test_apply_results_rejects_leaked_refusal_and_leaves_entry_translatable():
    # idは正規だがtgtがモデルの拒否理由の説明文になっているケース
    rows = [
        {"id": "a", "src": "Hello", "tgt": "", "status": "untranslated", "prev_tgt": None},
        {"id": "b", "src": "World", "tgt": "", "status": "untranslated", "prev_tgt": None},
    ]
    sent_ids = {"a", "b"}
    results = [
        {"id": "a", "tgt": "I cannot translate this content."},
        {"id": "b", "tgt": "世界"},
    ]

    outcome = translate.apply_results(
        rows, sent_ids, results, refusal_markers=("I cannot", "I can't")
    )

    assert rows[0]["status"] == "untranslated"
    assert rows[0]["tgt"] == ""
    assert rows[1]["tgt"] == "世界"
    assert outcome.applied == ["b"]
    assert outcome.refused_ids == ["a"]


def test_apply_results_clears_prev_tgt_and_ignores_write_back_by_position():
    # stale状態のエントリを、応答順が送信順と食い違う形で書き戻す
    rows = [
        {"id": "a", "src": "Hello there", "tgt": "こんにちは", "status": "stale", "prev_tgt": "こんにちは"},
        {"id": "b", "src": "Goodbye", "tgt": "", "status": "untranslated", "prev_tgt": None},
    ]
    sent_ids = {"a", "b"}
    # 応答はb, aの順（送信順と逆）で返ってきても、idで正しく引き当てられること
    results = [{"id": "b", "tgt": "さようなら"}, {"id": "a", "tgt": "やあ"}]

    translate.apply_results(rows, sent_ids, results)

    assert rows[0]["tgt"] == "やあ"
    assert rows[0]["prev_tgt"] is None
    assert rows[1]["tgt"] == "さようなら"


def test_build_user_content_includes_id_src_ctx_for_new_entry():
    chunk = [{"id": "a", "src": "Hello", "ctx": "greeting", "status": "untranslated", "prev_tgt": None}]

    content = translate.build_user_content(chunk)

    import json
    parsed = json.loads(content)
    assert parsed == {"lines": [{"id": "a", "src": "Hello", "ctx": "greeting"}]}


def test_build_user_content_includes_prev_tgt_hint_for_stale_entry():
    chunk = [
        {
            "id": "a",
            "src": "Hello there",
            "ctx": "greeting",
            "status": "stale",
            "prev_tgt": "こんにちは",
        }
    ]

    content = translate.build_user_content(chunk)

    import json
    parsed = json.loads(content)
    assert parsed == {
        "lines": [{"id": "a", "src": "Hello there", "ctx": "greeting", "prev_tgt": "こんにちは"}]
    }


def _row(id_, status="untranslated", scene=None, speaker=None, src=None, tgt=""):
    row = {"id": id_, "src": src or f"src-{id_}", "tgt": tgt, "ctx": "", "status": status,
           "hash": f"h-{id_}", "prev_tgt": None}
    if scene is not None:
        row["scene"] = scene
    if speaker is not None:
        row["speaker"] = speaker
    return row


def test_build_chunks_keeps_scene_rows_in_file_order_with_refs():
    # id を文字列ソートすると p10 が p2 より先に来る。行順（実行順）を保つこと
    rows = [
        _row("s/p2", scene="s"),
        _row("s/p10", status="locked", scene="s", tgt="原作"),
        _row("s/p11", scene="s"),
    ]

    chunks = translate.build_chunks(rows, chunk_size=30)

    assert [[r["id"] for r in c] for c in chunks] == [["s/p2", "s/p10", "s/p11"]]


def test_build_chunks_does_not_pull_identical_src_across_scenes():
    rows = [
        _row("a1", scene="a", src="Yes"),
        _row("a2", scene="a", src="No"),
        _row("b1", scene="b", src="Yes"),
    ]

    chunks = translate.build_chunks(rows, chunk_size=2)

    assert [[r["id"] for r in c] for c in chunks] == [["a1", "a2"], ["b1"]]


def test_build_chunks_splits_a_scene_when_targets_reach_chunk_size():
    rows = [
        _row("1", scene="s"),
        _row("2", status="locked", scene="s", tgt="x"),
        _row("3", scene="s"),
        _row("4", scene="s"),
        _row("5", status="locked", scene="s", tgt="y"),
    ]

    chunks = translate.build_chunks(rows, chunk_size=2)

    assert [[r["id"] for r in c] for c in chunks] == [["1", "2", "3"], ["4", "5"]]


def test_build_chunks_trims_refs_far_from_targets(monkeypatch):
    monkeypatch.setattr(translate, "CONTEXT_ROWS", 1)
    rows = [
        _row("far", status="locked", scene="s", tgt="x"),
        _row("near", status="locked", scene="s", tgt="y"),
        _row("t", scene="s"),
        _row("after", status="locked", scene="s", tgt="z"),
        _row("far2", status="locked", scene="s", tgt="w"),
    ]

    chunks = translate.build_chunks(rows, chunk_size=30)

    assert [[r["id"] for r in c] for c in chunks] == [["near", "t", "after"]]


def test_build_chunks_context_window_never_includes_targets_of_another_chunk():
    rows = [_row("1", scene="s"), _row("2", scene="s"), _row("3", scene="s")]

    chunks = translate.build_chunks(rows, chunk_size=1)

    assert [[r["id"] for r in c] for c in chunks] == [["1"], ["2"], ["3"]]


def test_build_chunks_does_not_merge_non_contiguous_runs_of_the_same_scene():
    rows = [_row("a1", scene="a"), _row("b1", scene="b"), _row("a2", scene="a")]

    chunks = translate.build_chunks(rows, chunk_size=30)

    assert [[r["id"] for r in c] for c in chunks] == [["a1"], ["b1"], ["a2"]]


def test_build_chunks_skips_scenes_without_targets():
    rows = [_row("1", status="locked", scene="done", tgt="x"), _row("2", scene="todo")]

    chunks = translate.build_chunks(rows, chunk_size=30)

    assert [[r["id"] for r in c] for c in chunks] == [["2"]]


def test_build_chunks_groups_identical_src_for_rows_without_scene():
    rows = [
        _row("1", src="OK"),
        _row("2", src="Cancel"),
        _row("3", src="OK"),
        _row("4", status="translated", src="Back", tgt="戻る"),
    ]

    chunks = translate.build_chunks(rows, chunk_size=2)

    assert [[r["id"] for r in c] for c in chunks] == [["1", "3"], ["2"]]


def test_build_user_content_sends_speaker_and_refs_without_id():
    chunk = [
        _row("1", status="locked", scene="s", speaker="Alice", src="Hi.", tgt="やあ。"),
        _row("2", scene="s", speaker="Bob", src="Hello."),
        _row("3", status="untranslated", scene="s", src="Empty ref"),  # 対象
    ]

    import json
    parsed = json.loads(translate.build_user_content(chunk))

    assert parsed["lines"] == [
        {"src": "Hi.", "tgt": "やあ。", "speaker": "Alice", "ref": True},
        {"id": "2", "src": "Hello.", "ctx": "", "speaker": "Bob"},
        {"id": "3", "src": "Empty ref", "ctx": ""},
    ]


def test_build_user_content_attaches_voices_only_for_speakers_in_chunk():
    samples = {"Alice": [{"src": "Hi.", "tgt": "やあ。"}], "Carol": [{"src": "Yo.", "tgt": "よう。"}]}
    chunk = [_row("2", scene="s", speaker="Alice"), _row("3", scene="s", speaker="Bob")]

    import json
    parsed = json.loads(translate.build_user_content(chunk, samples))

    assert parsed["voices"] == {"Alice": [{"src": "Hi.", "tgt": "やあ。"}]}


def test_build_user_content_omits_voices_when_no_samples_match():
    chunk = [_row("2", scene="s", speaker="Bob")]

    import json
    parsed = json.loads(translate.build_user_content(chunk, {}))

    assert "voices" not in parsed


def test_speaker_samples_collects_locked_lines_per_speaker_up_to_limit():
    rows = [
        _row("1", status="locked", speaker="Alice", src="a1", tgt="A1"),
        _row("2", status="translated", speaker="Alice", src="a2", tgt="A2"),  # 機械訳は手本にしない
        _row("3", status="locked", speaker="Alice", src="a3", tgt="A3"),
        _row("4", status="locked", speaker="Alice", src="a4", tgt="A4"),
        _row("5", status="locked", src="n", tgt="N"),  # 話者なし
    ]

    samples = translate.speaker_samples(rows, limit=2)

    assert samples == {"Alice": [{"src": "a1", "tgt": "A1"}, {"src": "a3", "tgt": "A3"}]}


def test_ref_lines_in_a_chunk_are_not_counted_as_sent():
    chunk = [_row("1", status="locked", scene="s", tgt="x"), _row("2", scene="s")]

    assert [r["id"] for r in translate.select_translatable(chunk)] == ["2"]


def test_save_pending_batch_records_only_target_rows(tmp_path):
    state_path = tmp_path / ".tl" / "batch-dialogue.json"
    chunk = [_row("1", status="locked", scene="s", tgt="x"), _row("2", scene="s")]

    translate.save_pending_batch(state_path, "batch_1", [chunk])

    assert translate.load_pending_batch(state_path)["sent_ids_by_chunk"] == {
        "chunk-0": [{"id": "2", "hash": "h-2"}]
    }


def test_build_system_blocks_tells_model_not_to_copy_english_structure():
    blocks = translate.build_system_blocks("方針", "用語集", [])
    text = blocks[0]["text"]

    assert "構文" in text
    assert "ref" in text
    assert "voices" in text


def test_build_system_blocks_caches_last_block():
    blocks = translate.build_system_blocks(
        style_guide="丁寧語で統一する。",
        glossary_tsv="Sword of Dawn\t暁の剣\t固有名詞",
        patterns=["\\{[^}]*\\}"],
    )

    assert blocks[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    joined = " ".join(b["text"] for b in blocks)
    assert "丁寧語で統一する" in joined
    assert "暁の剣" in joined


def test_parse_response_text_loads_json_array():
    text = '[{"id": "a", "tgt": "こんにちは"}]'

    assert translate.parse_response_text(text) == [{"id": "a", "tgt": "こんにちは"}]


def test_build_batch_requests_creates_one_request_per_chunk_with_custom_id():
    chunks = [
        [{"id": "a", "src": "Hello", "ctx": "", "status": "untranslated", "prev_tgt": None}],
        [{"id": "b", "src": "World", "ctx": "", "status": "untranslated", "prev_tgt": None}],
    ]
    system_blocks = translate.build_system_blocks("方針", "用語集", ["\\{[^}]*\\}"])
    cfg = {"model": "claude-opus-5"}

    requests = translate.build_batch_requests(chunks, cfg, system_blocks)

    assert [r["custom_id"] for r in requests] == ["chunk-0", "chunk-1"]
    assert requests[0]["params"]["model"] == "claude-opus-5"
    assert requests[0]["params"]["system"] == system_blocks
    assert requests[0]["params"]["output_config"]["format"]["type"] == "json_schema"


def test_cli_entrypoint_does_not_hit_module_not_found_when_run_as_a_script(tmp_path):
    """scripts/translate.py を直接実行しても `scripts` パッケージをimportできること。

    実行ディレクトリに依存して sys.path が変わり、CLIとしての実行だけが
    ImportError になる回帰を防ぐ。
    """
    import subprocess
    import sys as sys_mod
    from pathlib import Path as PathAlias

    translate_py = PathAlias(__file__).parent.parent / "scripts" / "translate.py"

    result = subprocess.run(
        [sys_mod.executable, str(translate_py), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),  # プラグインのルート以外の場所から実行しても壊れないことを確認
    )

    assert "ModuleNotFoundError" not in result.stderr


def test_save_pending_batch_then_load_pending_batch_roundtrips(tmp_path):
    state_path = tmp_path / ".tl" / "batch-dialogue.json"
    chunks = [
        [_row("a", src="Hi") | {"hash": "h1"}, _row("b", src="Bye") | {"hash": "h2"}],
        [_row("c", src="Yo") | {"hash": "h3"}],
    ]

    translate.save_pending_batch(state_path, "batch_123", chunks)
    loaded = translate.load_pending_batch(state_path)

    assert loaded["batch_id"] == "batch_123"
    assert loaded["sent_ids_by_chunk"] == {
        "chunk-0": [{"id": "a", "hash": "h1"}, {"id": "b", "hash": "h2"}],
        "chunk-1": [{"id": "c", "hash": "h3"}],
    }


def test_load_pending_batch_returns_none_when_no_state_file(tmp_path):
    assert translate.load_pending_batch(tmp_path / "does-not-exist.json") is None


def test_clear_pending_batch_removes_the_state_file(tmp_path):
    state_path = tmp_path / ".tl" / "batch-dialogue.json"
    translate.save_pending_batch(
        state_path, "batch_123", [[_row("a", src="Hi")]]
    )

    translate.clear_pending_batch(state_path)

    assert not state_path.exists()


def test_clear_pending_batch_is_a_noop_when_file_already_gone(tmp_path):
    translate.clear_pending_batch(tmp_path / "does-not-exist.json")  # 例外を投げないこと


def test_pending_batch_path_is_scoped_per_entries_file(tmp_path):
    project_dir = tmp_path

    path = translate.pending_batch_path(project_dir, project_dir / "entries" / "dialogue.jsonl")

    assert path == project_dir / ".tl" / "batch-dialogue.json"


class _FakeContentBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, cache_read=0, cache_creation=0):
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_creation


class _FakeMessage:
    def __init__(self, text, usage=None):
        self.content = [_FakeContentBlock(text)]
        self.usage = usage


class _FakeResult:
    def __init__(self, result_type, message=None):
        self.type = result_type
        self.message = message


class _FakeBatchResult:
    def __init__(self, custom_id, result):
        self.custom_id = custom_id
        self.result = result


class _FakeBatchesResults:
    def __init__(self, results):
        self._results = results

    def __call__(self, batch_id):
        return iter(self._results)


class _FakeClient:
    def __init__(self, results):
        self.messages = type("M", (), {})()
        self.messages.batches = type("B", (), {})()
        self.messages.batches.results = _FakeBatchesResults(results)


def test_fetch_and_apply_results_writes_back_by_custom_id_and_id():
    rows = [
        {"id": "a", "src": "Hi", "tgt": "", "status": "untranslated", "prev_tgt": None},
        {"id": "b", "src": "Yo", "tgt": "", "status": "untranslated", "prev_tgt": None},
    ]
    sent_ids_by_chunk = {"chunk-0": {"a"}, "chunk-1": {"b"}}
    client = _FakeClient(
        [
            _FakeBatchResult(
                "chunk-0",
                _FakeResult(
                    "succeeded",
                    _FakeMessage('[{"id": "a", "tgt": "やあ"}]', _FakeUsage(cache_read=100)),
                ),
            ),
            _FakeBatchResult(
                "chunk-1",
                _FakeResult(
                    "succeeded",
                    _FakeMessage('[{"id": "b", "tgt": "よう"}]', _FakeUsage(cache_read=50)),
                ),
            ),
        ]
    )

    outcome = translate.fetch_and_apply_results(client, "batch_x", rows, sent_ids_by_chunk)

    assert rows[0]["tgt"] == "やあ"
    assert rows[1]["tgt"] == "よう"
    assert outcome.cache_read_input_tokens == 150
    assert outcome.failed_chunks == []


def test_fetch_and_apply_results_records_failed_chunk_without_raising():
    rows = [{"id": "a", "src": "Hi", "tgt": "", "status": "untranslated", "prev_tgt": None}]
    sent_ids_by_chunk = {"chunk-0": {"a"}}
    client = _FakeClient([_FakeBatchResult("chunk-0", _FakeResult("errored"))])

    outcome = translate.fetch_and_apply_results(client, "batch_x", rows, sent_ids_by_chunk)

    assert rows[0]["status"] == "untranslated"
    assert outcome.failed_chunks == ["chunk-0 (errored)"]


def test_fetch_and_apply_results_passes_through_refusal_markers():
    rows = [{"id": "a", "src": "Hi", "tgt": "", "status": "untranslated", "prev_tgt": None}]
    sent_ids_by_chunk = {"chunk-0": {"a"}}
    client = _FakeClient(
        [
            _FakeBatchResult(
                "chunk-0",
                _FakeResult("succeeded", _FakeMessage('[{"id": "a", "tgt": "I cannot help"}]')),
            )
        ]
    )

    outcome = translate.fetch_and_apply_results(
        client, "batch_x", rows, sent_ids_by_chunk, refusal_markers=("I cannot",)
    )

    assert rows[0]["status"] == "untranslated"
    assert outcome.refused_ids == ["a"]


def test_resolve_pending_sent_ids_keeps_ids_whose_hash_is_unchanged():
    rows = [{"id": "a", "hash": "h1", "status": "untranslated"}]
    pending_sent_ids_by_chunk = {"chunk-0": [{"id": "a", "hash": "h1"}]}

    sent_ids_by_chunk, dropped = translate.resolve_pending_sent_ids(
        rows, pending_sent_ids_by_chunk
    )

    assert sent_ids_by_chunk == {"chunk-0": {"a"}}
    assert dropped == []


def test_resolve_pending_sent_ids_drops_ids_whose_source_changed_while_batch_was_in_flight():
    # バッチ投入後、tl-extractの再実行で原文が変わり、hashが送信時と食い違っている
    rows = [{"id": "a", "hash": "h1-new", "status": "stale"}]
    pending_sent_ids_by_chunk = {"chunk-0": [{"id": "a", "hash": "h1-old"}]}

    sent_ids_by_chunk, dropped = translate.resolve_pending_sent_ids(
        rows, pending_sent_ids_by_chunk
    )

    assert sent_ids_by_chunk == {"chunk-0": set()}
    assert dropped == ["a"]


def test_resolve_pending_sent_ids_drops_ids_no_longer_present_in_rows():
    rows: list[dict] = []  # idが行ごと削除された（あり得ないはずだが防御的に扱う）
    pending_sent_ids_by_chunk = {"chunk-0": [{"id": "a", "hash": "h1"}]}

    sent_ids_by_chunk, dropped = translate.resolve_pending_sent_ids(
        rows, pending_sent_ids_by_chunk
    )

    assert dropped == ["a"]


def test_apply_results_never_writes_translation_when_id_dropped_by_hash_mismatch():
    # resolve_pending_sent_idsでhash不一致により除外されたidは、応答が届いても
    # 「送っていないid」として扱われ、古い原文に対する訳が書き込まれてはならない
    rows = [
        {
            "id": "a",
            "src": "Hi there {name}",  # 新原文
            "tgt": "",
            "status": "stale",
            "prev_tgt": "こんにちは",
            "hash": "h1-new",
        }
    ]
    pending_sent_ids_by_chunk = {"chunk-0": [{"id": "a", "hash": "h1-old"}]}
    sent_ids_by_chunk, dropped = translate.resolve_pending_sent_ids(
        rows, pending_sent_ids_by_chunk
    )
    # モデルは旧原文("Hi")に対する訳で応答してくる
    results = [{"id": "a", "tgt": "こんにちは"}]

    outcome = translate.apply_results(rows, sent_ids_by_chunk["chunk-0"], results)

    assert rows[0]["tgt"] == ""  # 書き込まれていない
    assert rows[0]["status"] == "stale"  # ステータスも変わっていない
    assert outcome.rejected_unknown_ids == ["a"]
    assert dropped == ["a"]
