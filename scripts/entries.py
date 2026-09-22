"""中間フォーマット（JSONL）の入出力・hash計算・status遷移を扱う。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ENTRY_STATUSES = frozenset(
    {"untranslated", "translated", "reviewed", "stale", "needs-review", "locked"}
)


def hash_of(text: str) -> str:
    """原文のハッシュを返す。ゲーム更新時の差分検出に使う。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """JSONLファイルを読み込む。ファイルが無ければ空リストを返す。"""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def save_jsonl(path: str | Path, records: list[dict[str, Any]]) -> None:
    """JSONLファイルへ1レコード1行で書き出す。"""
    p = Path(path)
    with p.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


LOCATION_META_KEYS = ("scene", "speaker")


def _with_location_meta(entry: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """entryのscene/speakerをitemの値に置き換えた複製を返す（itemに無いキーは消す）。"""
    updated = {k: v for k, v in entry.items() if k not in LOCATION_META_KEYS}
    updated.update({k: item[k] for k in LOCATION_META_KEYS if k in item})
    return updated


def merge_extracted(
    existing: list[dict[str, Any]],
    extracted: list[dict[str, Any]],
    *,
    unfreeze_changed_locked: bool = False,
) -> list[dict[str, Any]]:
    """再抽出結果を既存エントリにマージする。

    - 新規id: untranslated として追加。ただしextractedのitemが"tgt"/"status"を
      持っていればそれを使う（公式ローカライズの一部流用・翻訳メモリ・原語版から
      確定訳が分かっている等、抽出アダプタが既知訳を提供できるケース向け）
    - hash不変: 既存エントリをそのまま維持
    - hash変化 (status != locked): 旧訳をprev_tgtへ退避してstaleにする。
      この時itemが"tgt"を持っていても無視する（人手編集の上書きを避けるため）。
      確定訳を凍結したいなら status に "locked" を渡す（locked分岐が先に短絡する）
    - status == locked: 原文が変わっても一切変更しない（凍結）。
      ただし unfreeze_changed_locked=True なら、hash が変わった locked も上の
      hash変化と同じく stale にする（MOD・ゲーム更新で原作訳の枠の原文が変わり、
      凍結したままだと旧版の原文のまま残って新版では原語が出てしまうケース向け）
    - extractedに無い既存id: そのまま残す（削除しない）
    - scene/speaker（任意）: 上のどの分岐でも itemの値で毎回置き換える（itemに無ければ
      キーを消す）。訳ではなく抽出位置のメタデータなので locked でも凍結しない。
      tl-translate が場面単位のチャンク化と話者の手本に使う
    """
    by_id = {e["id"]: e for e in existing}
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in extracted:
        entry_id = item["id"]
        seen_ids.add(entry_id)
        current = by_id.get(entry_id)
        new_hash = hash_of(item["src"])

        if current is None:
            status = item.get("status", "untranslated")
            if status not in ENTRY_STATUSES:
                raise ValueError(f"id={entry_id!r}: 未知のstatus {status!r}")
            merged.append(
                {
                    "id": entry_id,
                    "src": item["src"],
                    "tgt": item.get("tgt", ""),
                    "ctx": item.get("ctx", ""),
                    "status": status,
                    "hash": new_hash,
                    "prev_tgt": None,
                    "note": "",
                }
            )
        elif current["status"] == "locked" and not unfreeze_changed_locked:
            merged.append(current)
        elif current["hash"] == new_hash:
            merged.append(current)
        else:
            merged.append(
                {
                    **current,
                    "src": item["src"],
                    "ctx": item.get("ctx", current.get("ctx", "")),
                    "status": "stale",
                    "hash": new_hash,
                    "prev_tgt": current["tgt"],
                }
            )
        merged[-1] = _with_location_meta(merged[-1], item)

    for entry_id, current in by_id.items():
        if entry_id not in seen_ids:
            merged.append(current)

    return merged
