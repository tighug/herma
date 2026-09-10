"""fake_game 用の書き戻しアダプタ例。

extract.py の逆変換。元の辞書構造を保ちつつ、id で対応するエントリの訳文
(tgt。未翻訳ならsrc)を末端の値として書き戻す。
"""
from __future__ import annotations


def inject_into_dict(data: dict, entries_by_id: dict[str, dict], prefix: str = "") -> dict:
    """extract_from_dict と同じ規則でidを組み立て、対応する訳文を書き戻した新しい辞書を返す。"""
    result: dict = {}
    for key, value in data.items():
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(value, dict):
            result[key] = inject_into_dict(value, entries_by_id, path)
        else:
            entry = entries_by_id.get(path)
            result[key] = entry["tgt"] if entry and entry["tgt"] else value
    return result
