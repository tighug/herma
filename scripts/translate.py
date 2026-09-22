"""Message Batches API によるバッチ翻訳ランナー。

対象選定・チャンク化・id照合による書き戻しは、ネットワークを介さずテストできる
純粋関数として実装する。バッチ投入・ポーリング・結果取得はAnthropic SDKへの
薄いラッパーとして分離する。
"""
from __future__ import annotations

import itertools
import json
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
    """エントリをchunk_size件ずつのチャンクに分割する。

    同一srcは可能な限り同じチャンクに寄せる。別チャンクに散ると、モデルが
    別々に訳して訳ゆれが発生するため（実測: 訳ゆれ後始末だけで2,492+717+33グループ）。
    単独でchunk_sizeを超えるグループはそのグループ内で分割する（1リクエストの
    max_tokensを超えて丸ごと失敗しないように）。
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["src"], []).append(row)

    chunks: list[list[dict]] = []
    current: list[dict] = []
    for group in groups.values():
        if len(group) > chunk_size:
            if current:
                chunks.append(current)
                current = []
            chunks.extend(group[i : i + chunk_size] for i in range(0, len(group), chunk_size))
            continue
        if current and len(current) + len(group) > chunk_size:
            chunks.append(current)
            current = []
        current.extend(group)
    if current:
        chunks.append(current)
    return chunks


# 翻訳対象の前後に同梱する参照行の上限。場面全体を送ると locked の多い原作訳流用
# プロジェクトで入力が膨らむ（celesphonia 実測: 対象1,107行に対し場面全体で参照行約4,400行）
CONTEXT_ROWS = 10


def build_chunks(rows: list[dict], chunk_size: int) -> list[list[dict]]:
    """1ファイル分の全行からチャンクを作る。行順（=抽出アダプタが出した実行順）は崩さない。

    - sceneを持つ行: 同じsceneが連続する行の並びを1場面とし、翻訳対象をchunk_size件ずつに
      区切る。各チャンクには対象の前後CONTEXT_ROWS行までの非対象行（locked/translated等）を
      参照行として同梱する（前後の文脈と原作訳の手本になる）。窓は隣のチャンクの対象の手前で
      止め、同じ対象を2つのチャンクに入れない。同一srcの集約はしない —
      集約すると場面が切り刻まれ、行単位の直訳（she→「彼女」等）に戻るため
    - sceneを持たない行（UI文字列など）: 従来どおり chunk_entries で同一srcを集約する
    - 翻訳対象の無いsceneはチャンクにしない
    """
    chunks: list[list[dict]] = []
    unscened: list[dict] = []
    for scene, run in itertools.groupby(rows, key=lambda r: r.get("scene")):
        run = list(run)
        if scene is None:
            unscened.extend(select_translatable(run))
            continue
        idx = [i for i, r in enumerate(run) if r["status"] in TRANSLATABLE_STATUSES]
        for start in range(0, len(idx), chunk_size):
            part = idx[start : start + chunk_size]
            lo = max(part[0] - CONTEXT_ROWS, idx[start - 1] + 1 if start else 0)
            end = start + chunk_size
            hi = min(part[-1] + CONTEXT_ROWS + 1, idx[end] if end < len(idx) else len(run))
            chunks.append(run[lo:hi])
    return chunks + chunk_entries(unscened, chunk_size)


def speaker_samples(rows: list[dict], limit: int = 10) -> dict[str, list[dict]]:
    """話者ごとに原作訳（locked）の行を最大limit件集める。モデルに渡す口調の手本。"""
    samples: dict[str, list[dict]] = {}
    for row in rows:
        speaker = row.get("speaker")
        if not speaker or row["status"] != "locked" or not row.get("tgt"):
            continue
        bucket = samples.setdefault(speaker, [])
        if len(bucket) < limit:
            bucket.append({"src": row["src"], "tgt": row["tgt"]})
    return samples


@dataclass
class ApplyOutcome:
    applied: list[str] = field(default_factory=list)
    rejected_unknown_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    failed_chunks: list[str] = field(default_factory=list)
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    dropped_stale_hash_ids: list[str] = field(default_factory=list)
    refused_ids: list[str] = field(default_factory=list)


# idは正規なのにtgtがモデルの拒否理由の説明文になっているケースの既定マーカー。
# 英語の定型句のみを持つ（日本語の拒否文は翻訳方向に依存するため tl.config.json の
# refusal_markers で上書きする）。
DEFAULT_REFUSAL_MARKERS = ("I cannot", "I can't", "I'm not able", "I apologize")


def looks_like_leaked_refusal(tgt: str, markers: tuple[str, ...]) -> bool:
    """idは正規なのに、tgtが拒否理由の説明文になっている応答を検出する。"""
    return any(marker in tgt for marker in markers)


def apply_results(
    rows: list[dict],
    sent_ids: set[str],
    results: list[dict],
    refusal_markers: tuple[str, ...] = DEFAULT_REFUSAL_MARKERS,
) -> ApplyOutcome:
    """バッチ応答をid照合で書き戻す。位置での書き戻しは行わない。

    - sent_idsに無いidの応答は破棄する（rejected_unknown_ids に記録）
    - sent_idsにあるのに応答が無いidはuntranslated/staleのまま据え置く（missing_ids に記録）
    - tgtが拒否理由の説明文に見えるidは書き込まず、untranslated/staleのまま
      据え置く（refused_ids に記録）。次回実行で再度翻訳対象になる
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
        if looks_like_leaked_refusal(item["tgt"], refusal_markers):
            outcome.refused_ids.append(entry_id)
            continue
        row["tgt"] = item["tgt"]
        row["status"] = "translated"
        row["prev_tgt"] = None
        outcome.applied.append(entry_id)

    outcome.missing_ids = sorted(sent_ids - responded_ids)
    return outcome


def build_user_content(chunk: list[dict], samples: dict[str, list[dict]] | None = None) -> str:
    """チャンクをモデルに渡すJSON文字列に変換する。

    - 翻訳対象: id/src/ctx（+speaker）。staleには旧訳(prev_tgt)を差分翻訳のヒントに添える
    - 参照行（訳のある非対象行）: idを付けず ref=true で渡す。書き戻し対象にならない
    - voices: チャンクに出る話者の原作訳の手本（speaker_samples の結果から抜き出す）
    """
    lines = []
    for entry in chunk:
        if entry["status"] in TRANSLATABLE_STATUSES:
            item = {"id": entry["id"], "src": entry["src"], "ctx": entry.get("ctx", "")}
            if entry.get("status") == "stale" and entry.get("prev_tgt"):
                item["prev_tgt"] = entry["prev_tgt"]
        elif entry.get("tgt"):
            item = {"src": entry["src"], "tgt": entry["tgt"], "ref": True}
        else:
            continue
        if entry.get("speaker"):
            item["speaker"] = entry["speaker"]
        lines.append(item)

    content: dict = {"lines": lines}
    speakers = dict.fromkeys(e["speaker"] for e in chunk if e.get("speaker"))
    samples = samples or {}
    voices = {s: samples[s] for s in speakers if s in samples}
    if voices:
        content["voices"] = voices
    return json.dumps(content, ensure_ascii=False)


# 英語の直訳調を避ける、ゲームに依存しない原則。unholy-maiden-sop-jp の再翻訳パスで
# 直訳調が消えたプロンプトから、ゲーム固有の数値・語彙を除いたもの。
NATURALNESS_RULES = """## 訳し方の原則
- 英語原文は「何が起きて、誰が何を言ったか」を知るための材料。語順・構文・単語の対応は写さない
- 場面を最初から最後まで読んでから訳す。前後の行と一続きの日本語になるようにする
- 日本語で要らない主語・代名詞・所有格は書かない（she/her を毎回「彼女」、your を「あなたの」と訳さない。
  「彼女の髪に」→「髪に」）。前の行で分かっている主語・目的語は繰り返さない
- 英語の慣用句・比喩・間投詞・罵倒は字義どおりに訳さず、日本語で同じ場面に言うことばに置き換える
  （×「おお、神よ」 Oh god）
- 直訳構文を使わない: 「〜することができる」「〜する必要がある」「〜というわけじゃない」(It's not like)
  「〜させてくれ」(let me)、無生物主語（×「その光景が彼女を興奮させた」→○「その光景に興奮した」）、
  by の受け身（×「男によって押し倒された」→○「男に押し倒された」）
- just / so / very / please / and などを毎回「ただ」「とても」「お願い」「そして」と訳さない。
  強調は語尾や言い回しで出す
- 1つの英文を2つに割る、2つを1つにまとめる、語順を入れ替える、は自由。ただし行（id）をまたいで
  内容を動かさない（各行はゲーム内で別々に表示される）
- 意味と情報量は保つ。語を削って意味や文法が欠けてはいけない
"""


def build_system_blocks(style_guide: str, glossary_tsv: str, patterns: list[str]) -> list[dict]:
    """全リクエスト共通のsystemプロンプトを組み立てる。末尾にキャッシュを付ける。"""
    instructions = (
        "あなたはゲームのローカライズ翻訳者です。原文を、最初から日本語で書かれていたかのような"
        "日本語にしてください。\n\n"
        f"{NATURALNESS_RULES}\n"
        "以下はこのプロジェクト固有の方針です。上の原則と食い違う場合はこちらを優先してください。\n\n"
        f"## 翻訳方針\n{style_guide}\n\n"
        f"## 用語集（原文 / 訳語 / 備考）\n{glossary_tsv}\n\n"
        "## プレースホルダーの扱い\n"
        f"以下の正規表現にマッチするトークンは翻訳せず、原文のまま訳文に残してください: {patterns}\n\n"
        "## 入出力形式\n"
        '入力は {"lines": [...], "voices": {...}} のJSONです。\n'
        '- lines: 場面の行を実行順に並べたもの。id のある行 {"id", "src", "ctx", "speaker", "prev_tgt"} が'
        "翻訳対象。prev_tgt は原文が変わる前の旧訳（差分のヒント）\n"
        '- ref が true の行 {"src", "tgt", "speaker"} は訳済みの行（原作の訳を含む）。訳し直さず、'
        "前後の文脈と文体の手本として読む\n"
        "- voices: 話者ごとの原作の台詞（src → tgt）。この話者の一人称・語尾・口調はこれに合わせる\n"
        "- speaker・voices・prev_tgt は無いことがある\n"
        '出力は翻訳対象の行だけを [{"id": ..., "tgt": ...}] のJSON配列で返してください。'
    )
    return [
        {"type": "text", "text": instructions, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]


def build_batch_requests(
    chunks: list[list[dict]],
    cfg: dict,
    system_blocks: list[dict],
    samples: dict[str, list[dict]] | None = None,
) -> list:
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
                    messages=[{"role": "user", "content": build_user_content(chunk, samples)}],
                    output_config={
                        "format": {"type": "json_schema", "schema": RESULT_JSON_SCHEMA}
                    },
                ),
            )
        )
    return requests


def parse_response_text(text: str) -> list[dict]:
    """モデル応答のテキストからJSON配列を取り出す。

    output_config.format(json_schema) 指定時、最初のテキストブロックは
    有効なJSONであることがAPI仕様として保証されるため、素の json.loads で十分。
    """
    return json.loads(text)


def pending_batch_path(project_dir, jsonl_path) -> Path:
    """entries/*.jsonl 1ファイルにつき1つの実行状態ファイルのパスを返す。"""
    return Path(project_dir) / ".tl" / f"batch-{Path(jsonl_path).stem}.json"


def save_pending_batch(state_path, batch_id: str, chunks: list[list[dict]]) -> None:
    """投入済みバッチのIDと送信id・送信時hashを保存する。中断後の再アタッチに使う。

    hashも一緒に保存するのは、再アタッチまでの間に tl-extract が再実行されて
    原文が変わっていた場合に検出するため（resolve_pending_sent_ids参照）。
    チャンク内の参照行（build_chunks参照）は送信対象ではないので記録しない。
    """
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "batch_id": batch_id,
        "sent_ids_by_chunk": {
            f"chunk-{i}": [{"id": e["id"], "hash": e["hash"]} for e in select_translatable(chunk)]
            for i, chunk in enumerate(chunks)
        },
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_pending_sent_ids(
    rows: list[dict], pending_sent_ids_by_chunk: dict[str, list[dict]]
) -> tuple[dict[str, set[str]], list[str]]:
    """再アタッチ時、送信時hashと現在のrowのhashを突き合わせる。

    バッチ投入後に tl-extract が再実行されて原文が変わった（hashが違う）idや、
    rowsから消えたidは「送信済み」から除外する。除外されたidは、モデルから
    応答が届いても apply_results 側で「送っていないid」として拒否され、
    古い原文に対する訳が書き込まれるのを防ぐ。
    """
    by_id = {r["id"]: r for r in rows}
    filtered: dict[str, set[str]] = {}
    dropped: list[str] = []

    for chunk_key, sent_entries in pending_sent_ids_by_chunk.items():
        kept: set[str] = set()
        for sent in sent_entries:
            row = by_id.get(sent["id"])
            if row is not None and row.get("hash") == sent["hash"]:
                kept.add(sent["id"])
            else:
                dropped.append(sent["id"])
        filtered[chunk_key] = kept

    return filtered, dropped


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


def wait_for_batch(client, batch_id: str, poll_interval_sec: float = 10.0):
    """バッチが完了(processing_status == 'ended')するまでポーリングする。"""
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        time.sleep(poll_interval_sec)


def fetch_and_apply_results(
    client,
    batch_id: str,
    rows: list[dict],
    sent_ids_by_chunk: dict[str, set[str]],
    refusal_markers: tuple[str, ...] = DEFAULT_REFUSAL_MARKERS,
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
        except json.JSONDecodeError:
            total.failed_chunks.append(f"{result.custom_id} (unparsable response)")
            continue
        outcome = apply_results(rows, sent_ids, parsed, refusal_markers=refusal_markers)
        total.applied.extend(outcome.applied)
        total.rejected_unknown_ids.extend(outcome.rejected_unknown_ids)
        total.missing_ids.extend(outcome.missing_ids)
        total.refused_ids.extend(outcome.refused_ids)

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
    refusal_markers = tuple(cfg.get("refusal_markers", DEFAULT_REFUSAL_MARKERS))

    client = anthropic.Anthropic()
    total = ApplyOutcome()

    for jsonl_path in sorted((project_dir / "entries").glob("*.jsonl")):
        rows = entries_mod.load_jsonl(jsonl_path)
        state_path = pending_batch_path(project_dir, jsonl_path)
        pending = load_pending_batch(state_path)

        if pending is not None:
            # 前回の実行が中断していた場合、新規投入せず同じバッチに再アタッチする
            # （バッチは最大24時間かかり得るため、ここで再投入すると二重課金になる）。
            # 再アタッチまでの間にtl-extractが再実行され原文が変わったidは、
            # 古い原文への訳を書き込まないよう送信済み扱いから除外する
            batch_id = pending["batch_id"]
            sent_ids_by_chunk, dropped = resolve_pending_sent_ids(
                rows, pending["sent_ids_by_chunk"]
            )
            total.dropped_stale_hash_ids.extend(dropped)
        else:
            chunks = build_chunks(rows, cfg.get("chunk_size", 30))
            if not chunks:
                continue
            requests = build_batch_requests(chunks, cfg, system_blocks, speaker_samples(rows))
            batch_id = client.messages.batches.create(requests=requests).id
            sent_ids_by_chunk = {
                f"chunk-{i}": {e["id"] for e in select_translatable(chunk)}
                for i, chunk in enumerate(chunks)
            }
            save_pending_batch(state_path, batch_id, chunks)

        wait_for_batch(client, batch_id)
        outcome = fetch_and_apply_results(
            client, batch_id, rows, sent_ids_by_chunk, refusal_markers=refusal_markers
        )

        entries_mod.save_jsonl(jsonl_path, rows)
        clear_pending_batch(state_path)
        total.applied.extend(outcome.applied)
        total.rejected_unknown_ids.extend(outcome.rejected_unknown_ids)
        total.missing_ids.extend(outcome.missing_ids)
        total.failed_chunks.extend(outcome.failed_chunks)
        total.refused_ids.extend(outcome.refused_ids)
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
    if outcome.refused_ids:
        print(f"警告: モデルが翻訳を拒否したid（未訳のまま。再実行で拾い直せます）: {outcome.refused_ids}")
    if outcome.failed_chunks:
        print(f"警告: 失敗したチャンク: {outcome.failed_chunks}")
    if outcome.dropped_stale_hash_ids:
        print(
            "警告: 再アタッチ中に原文が変わっていたため翻訳を破棄したid "
            f"（tl-extract→tl-translateを再実行してください）: {outcome.dropped_stale_hash_ids}"
        )
    print(
        f"キャッシュ: read={outcome.cache_read_input_tokens} "
        f"creation={outcome.cache_creation_input_tokens}"
    )
