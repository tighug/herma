"""Message Batches API によるバッチ翻訳ランナー。

対象選定・チャンク化・id照合による書き戻しは、ネットワークを介さずテストできる
純粋関数として実装する。バッチ投入・ポーリング・結果取得はAnthropic SDKへの
薄いラッパーとして分離する。
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# `python .../scripts/translate.py` で直接実行すると sys.path[0] が scripts/ になり
# `from scripts import entries` が解決できない。プラグインルート（scripts/の親）を
# 明示的にsys.pathへ足しておくことで、importの形を実行方法に依存させない。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

TRANSLATABLE_STATUSES = {"untranslated", "stale"}

RESULT_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "tgt": {"type": "string"}},
        "required": ["id", "tgt"],
        "additionalProperties": False,
    },
}


def select_translatable(rows: list[dict]) -> list[dict]:
    """翻訳対象（untranslated/stale）のみを抽出する。lockedは常に除外する。"""
    return [r for r in rows if r["status"] in TRANSLATABLE_STATUSES]


def chunk_entries(rows: list[dict], chunk_size: int) -> list[list[dict]]:
    """エントリをchunk_size件ずつのチャンクに分割する。"""
    return [rows[i : i + chunk_size] for i in range(0, len(rows), chunk_size)]


@dataclass
class ApplyOutcome:
    applied: list[str] = field(default_factory=list)
    rejected_unknown_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    failed_chunks: list[str] = field(default_factory=list)
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def apply_results(
    rows: list[dict], sent_ids: set[str], results: list[dict]
) -> ApplyOutcome:
    """バッチ応答をid照合で書き戻す。位置での書き戻しは行わない。

    - sent_idsに無いidの応答は破棄する（rejected_unknown_ids に記録）
    - sent_idsにあるのに応答が無いidはuntranslated/staleのまま据え置く（missing_ids に記録）
    """
    by_id = {r["id"]: r for r in rows}
    outcome = ApplyOutcome()
    responded_ids: set[str] = set()

    for item in results:
        entry_id = item["id"]
        if entry_id not in sent_ids:
            outcome.rejected_unknown_ids.append(entry_id)
            continue
        responded_ids.add(entry_id)
        row = by_id.get(entry_id)
        if row is None:
            outcome.rejected_unknown_ids.append(entry_id)
            continue
        row["tgt"] = item["tgt"]
        row["status"] = "translated"
        row["prev_tgt"] = None
        outcome.applied.append(entry_id)

    outcome.missing_ids = sorted(sent_ids - responded_ids)
    return outcome


def build_user_content(chunk: list[dict]) -> str:
    """チャンクをモデルに渡すJSON文字列に変換する。

    staleエントリには旧訳(prev_tgt)を添え、差分翻訳のヒントにする。
    """
    items = []
    for entry in chunk:
        item = {"id": entry["id"], "src": entry["src"], "ctx": entry.get("ctx", "")}
        if entry.get("status") == "stale" and entry.get("prev_tgt"):
            item["prev_tgt"] = entry["prev_tgt"]
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def build_system_blocks(style_guide: str, glossary_tsv: str, patterns: list[str]) -> list[dict]:
    """全リクエスト共通のsystemプロンプトを組み立てる。末尾にキャッシュを付ける。"""
    instructions = (
        "あなたはゲームのローカライズ翻訳者です。以下の方針に従って翻訳してください。\n\n"
        f"## 翻訳方針\n{style_guide}\n\n"
        f"## 用語集（原文 / 訳語 / 備考）\n{glossary_tsv}\n\n"
        "## プレースホルダーの扱い\n"
        f"以下の正規表現にマッチするトークンは翻訳せず、原文のまま訳文に残してください: {patterns}\n\n"
        "## 出力形式\n"
        '入力は [{"id": ..., "src": ..., "ctx": ..., "prev_tgt": ...}] のJSON配列です。'
        '出力は [{"id": ..., "tgt": ...}] のJSON配列のみを返してください。'
    )
    return [
        {"type": "text", "text": instructions, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]


def build_batch_requests(chunks: list[list[dict]], cfg: dict, system_blocks: list[dict]) -> list:
    """チャンクごとにMessage Batches APIのRequestを組み立てる。custom_idはチャンク番号。"""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests = []
    for i, chunk in enumerate(chunks):
        requests.append(
            Request(
                custom_id=f"chunk-{i}",
                params=MessageCreateParamsNonStreaming(
                    model=cfg["model"],
                    max_tokens=16000,
                    system=system_blocks,
                    messages=[{"role": "user", "content": build_user_content(chunk)}],
                    output_config={
                        "format": {"type": "json_schema", "schema": RESULT_JSON_SCHEMA}
                    },
                ),
            )
        )
    return requests


def parse_response_text(text: str) -> list[dict]:
    """モデル応答のテキストからJSON配列を取り出す。前後に余計な文章があっても抽出する。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def pending_batch_path(project_dir, jsonl_path) -> Path:
    """entries/*.jsonl 1ファイルにつき1つの実行状態ファイルのパスを返す。"""
    return Path(project_dir) / ".tl" / f"batch-{Path(jsonl_path).stem}.json"


def save_pending_batch(state_path, batch_id: str, chunks: list[list[dict]]) -> None:
    """投入済みバッチのIDと送信idを保存する。中断後の再アタッチに使う。"""
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "batch_id": batch_id,
        "sent_ids_by_chunk": {
            f"chunk-{i}": [e["id"] for e in chunk] for i, chunk in enumerate(chunks)
        },
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pending_batch(state_path) -> dict | None:
    """実行状態ファイルを読み込む。存在しなければNone。"""
    state_path = Path(state_path)
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def clear_pending_batch(state_path) -> None:
    """完了したバッチの実行状態ファイルを削除する。無ければ何もしない。"""
    state_path = Path(state_path)
    if state_path.exists():
        state_path.unlink()


# --- ここから先はAnthropic SDKへの薄いラッパー（ネットワークI/O） ---


def create_batch(client, requests: list) -> str:
    """バッチを投入し、バッチIDを返す。"""
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def wait_for_batch(client, batch_id: str, poll_interval_sec: float = 10.0, sleep=None):
    """バッチが完了(processing_status == 'ended')するまでポーリングする。"""
    _sleep = sleep or time.sleep
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        _sleep(poll_interval_sec)


def fetch_and_apply_results(
    client, batch_id: str, rows: list[dict], sent_ids_by_chunk: dict[str, set[str]]
) -> ApplyOutcome:
    """バッチ結果をcustom_idでチャンクに、idでエントリに突き合わせて書き戻す。

    sent_ids_by_chunk はチャンクのcustom_id（例: "chunk-0"）から、そのチャンクで
    送信したidの集合への対応。新規実行でもバッチ状態からの再アタッチでも同じ形で渡せる。
    """
    total = ApplyOutcome()

    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            total.failed_chunks.append(f"{result.custom_id} ({result.result.type})")
            continue

        message = result.result.message
        if message.usage is not None:
            total.cache_read_input_tokens += message.usage.cache_read_input_tokens or 0
            total.cache_creation_input_tokens += (
                message.usage.cache_creation_input_tokens or 0
            )

        sent_ids = sent_ids_by_chunk.get(result.custom_id, set())
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            parsed = parse_response_text(text)
        except (json.JSONDecodeError, StopIteration):
            total.failed_chunks.append(f"{result.custom_id} (unparsable response)")
            continue
        outcome = apply_results(rows, sent_ids, parsed)
        total.applied.extend(outcome.applied)
        total.rejected_unknown_ids.extend(outcome.rejected_unknown_ids)
        total.missing_ids.extend(outcome.missing_ids)

    return total


def load_config(config_path) -> dict:
    """tl.config.json を読み込む。"""
    return json.loads(Path(config_path).read_text(encoding="utf-8"))


def run(project_dir) -> ApplyOutcome:
    """プロジェクトディレクトリを1つ受け取り、抽出→バッチ翻訳→書き戻しまでを行う。

    entries/*.jsonl を全て読み込み、ファイルごとに独立して処理し、同じファイルへ書き戻す。
    """
    import anthropic

    from scripts import entries as entries_mod

    project_dir = Path(project_dir)
    cfg = load_config(project_dir / "tl.config.json")
    style_guide = (project_dir / "CLAUDE.md").read_text(encoding="utf-8")
    glossary_tsv = (project_dir / "glossary.tsv").read_text(encoding="utf-8")
    system_blocks = build_system_blocks(style_guide, glossary_tsv, cfg["placeholder_patterns"])

    client = anthropic.Anthropic()
    total = ApplyOutcome()

    for jsonl_path in sorted((project_dir / "entries").glob("*.jsonl")):
        rows = entries_mod.load_jsonl(jsonl_path)
        state_path = pending_batch_path(project_dir, jsonl_path)
        pending = load_pending_batch(state_path)

        if pending is not None:
            # 前回の実行が中断していた場合、新規投入せず同じバッチに再アタッチする
            # （バッチは最大24時間かかり得るため、ここで再投入すると二重課金になる）
            batch_id = pending["batch_id"]
            sent_ids_by_chunk = {k: set(v) for k, v in pending["sent_ids_by_chunk"].items()}
        else:
            targets = select_translatable(rows)
            if not targets:
                continue
            chunks = chunk_entries(targets, cfg.get("chunk_size", 30))
            requests = build_batch_requests(chunks, cfg, system_blocks)
            batch_id = create_batch(client, requests)
            sent_ids_by_chunk = {
                f"chunk-{i}": {e["id"] for e in chunk} for i, chunk in enumerate(chunks)
            }
            save_pending_batch(state_path, batch_id, chunks)

        wait_for_batch(client, batch_id)
        outcome = fetch_and_apply_results(client, batch_id, rows, sent_ids_by_chunk)

        entries_mod.save_jsonl(jsonl_path, rows)
        clear_pending_batch(state_path)
        total.applied.extend(outcome.applied)
        total.rejected_unknown_ids.extend(outcome.rejected_unknown_ids)
        total.missing_ids.extend(outcome.missing_ids)
        total.failed_chunks.extend(outcome.failed_chunks)
        total.cache_read_input_tokens += outcome.cache_read_input_tokens
        total.cache_creation_input_tokens += outcome.cache_creation_input_tokens

    return total


if __name__ == "__main__":
    outcome = run(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(f"翻訳完了: {len(outcome.applied)}件")
    if outcome.rejected_unknown_ids:
        print(f"警告: 不明なidの応答を破棄しました: {outcome.rejected_unknown_ids}")
    if outcome.missing_ids:
        print(f"警告: 応答が無かったid（未訳のまま）: {outcome.missing_ids}")
    if outcome.failed_chunks:
        print(f"警告: 失敗したチャンク: {outcome.failed_chunks}")
    print(
        f"キャッシュ: read={outcome.cache_read_input_tokens} "
        f"creation={outcome.cache_creation_input_tokens}"
    )
