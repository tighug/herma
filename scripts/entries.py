"""中間フォーマット（JSONL）の入出力・hash計算・status遷移を扱う。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


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


def merge_extracted(
    existing: list[dict[str, Any]], extracted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """再抽出結果を既存エントリにマージする。

    - 新規id: untranslated として追加
    - hash不変: 既存エントリをそのまま維持
    - hash変化 (status != locked): 旧訳をprev_tgtへ退避してstaleにする
    - status == locked: 原文が変わっても一切変更しない（凍結）
    - extractedに無い既存id: そのまま残す（削除しない）
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
            merged.append(
                {
                    "id": entry_id,
                    "src": item["src"],
                    "tgt": "",
                    "ctx": item.get("ctx", ""),
                    "status": "untranslated",
                    "hash": new_hash,
                    "prev_tgt": None,
                    "note": "",
                }
            )
        elif current["status"] == "locked":
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

    for entry_id, current in by_id.items():
        if entry_id not in seen_ids:
            merged.append(current)

    return merged
