"""fake_game 用の抽出アダプタ例。

ネストした辞書を再帰的に走査し、末端の文字列を {id, src, ctx} 行に変換する。
id はキーパス（"/"区切り）そのもの、ctx は親キーパス。
実ゲームでは tl-extract スキルがこのファイルを実装例として、対象ゲームの
実際のデータ構造に合わせたアダプタを書く。

**制限**: これは辞書のネストのみを扱う最小限の例で、リスト（配列）は扱わない。
リストを含む形式（会話の配列など）を抽出する場合は、対象ゲームのアダプタで
リストのインデックスもidパスに含める処理を追加すること。
"""
from __future__ import annotations


def extract_from_dict(data: dict, prefix: str = "") -> list[dict]:
    """ネストした辞書から {id, src, ctx} のリストを作る。"""
    rows: list[dict] = []
    for key, value in data.items():
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(value, dict):
            rows.extend(extract_from_dict(value, path))
        else:
            rows.append({"id": path, "src": value, "ctx": prefix})
    return rows
