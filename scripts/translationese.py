"""翻訳調（英語の直訳っぽさ）を、原作訳（locked）を物差しにして測る（英→日）。

全体の出現率で比べると、文の種類の偏り（三人称のログが多い等）と翻訳調が混ざる。
そこで**英語の原文にその語がある行だけ**を母数にして、訳文にその訳語が出る率を
locked（原作の訳）と機械翻訳（locked 以外で訳がある行）で比べる
（例: she/her を含む行で「彼女」と訳した率 原作5% / 機械翻訳26%）。

- 機械翻訳の行が十分あれば、原作より明らかに多く訳出している語を「採用」し、該当行にタグを付ける
- 機械翻訳の行が無い（翻訳前の）プロジェクトでは、原作が訳さずに済ませている語（avoided）を出す。
  翻訳方針（CLAUDE.md）の「訳さずに済ませる語」に書き、翻訳調を予防するのに使う

測って出力するだけで、entries の status は一切変えない（数千行を needs-review に落とさないため）。
出力は qa/translationese.json と qa/translationese-report.md。

    uv run --project <PLUGIN_ROOT> python <PLUGIN_ROOT>/scripts/translationese.py <PROJECT>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# translate.py と同じ理由（直接実行時に `from scripts import ...` を解決するため）
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from scripts import entries as entries_mod  # noqa: E402

# (訳語のキー, 英語原文の正規表現, 訳文の正規表現)。ジャンルに依存しない一般語だけを置く。
# 罵倒語・喘ぎ・擬音などは作品ごとに tl.config.json の translationese.extra_calques で足す。
DEFAULT_CALQUES: list[tuple[str, str, str]] = [
    ("彼女", r"\b(she|her)\b", r"彼女"),
    ("彼", r"\b(he|him|his)\b", r"彼(?!女)"),
    # 「彼女の髪」→「その髪」は代名詞の直訳が形を変えただけ。書き直すとこちらへ寄りやすい
    ("その", r"\b(she|her)\b", r"その"),
    ("ただ", r"\bjust\b", r"ただ"),
    ("本当に", r"\breally\b", r"本当"),
    ("まるで", r"\blike an?\b|\bas if\b|\bas though\b", r"まるで"),
    ("とても", r"\b(so|very)\b", r"とても|すごく|非常に"),
    ("あなたの", r"\byour\b", r"あなたの|貴方の|お前の|君の"),
    ("私の", r"\bmy\b", r"私の"),
    ("それ", r"\bit\b", r"それ"),
    ("すべて", r"\b(every\w*|all)\b", r"すべて|全て|全部"),
    ("お願い", r"\bplease\b", r"お願い"),
    ("感じ", r"\bfeel\w*\b", r"感じ"),
    ("味わ", r"\bfeel\w*\b", r"味わ"),
    ("決して", r"\bnever\b", r"決して"),
    ("今まで", r"\bever\b", r"今まで|これまで"),
    ("完全に", r"\b(completely|totally|utterly)\b", r"完全に|すっかり"),
    ("そして", r"\band\b", r"そして"),
]

# 採用の閾値: 機械翻訳の率が原作の2倍以上、かつ5ポイント以上高く、母数が両側とも30行以上。
# 倍率だけだと低い率同士の差（1%→3%）を拾い、差だけだと元々よく訳す語を拾う。
# 原作側の母数も要るのは、原作訳が数十行しかない作品で「原作 0%（11行中）」を物差しにしないため。
MIN_RATIO = 2.0
MIN_GAP = 0.05
MIN_N = 30
# 原作が「訳さずに済ませている」とみなす率（原作の母数 MIN_N 以上）
AVOIDED_MAX = 0.15

# 地の文の組み立ての翻訳調。機械翻訳は英語の文の切れ目を読点でつなぎ、進行形を「〜ている」で写しやすい。
# 語と違って原文側の条件が無いので、同じ地の文の条件で原作と率を比べる（ある作品の実測では
# 1行に読点2つ以上 原作3% / 機械翻訳18%、〜ている2つ以上 0% / 5%）
_COMMA_LINE = 2
_TEIRU = re.compile(r"ている|ていく|てくる|ていた|ていっ")
NARRATION_MIN_LEN = 20

# 測る側から外す status。stale は旧訳が残っているだけ、untranslated は訳が無い
_NOT_MEASURED = {"locked", "untranslated", "stale"}


class Config:
    """tl.config.json の translationese 節と placeholder_patterns から作る設定。"""

    def __init__(self, cfg: dict):
        t = cfg.get("translationese", {})
        calques = DEFAULT_CALQUES + [tuple(c) for c in t.get("extra_calques", [])]
        self.calques = {k: (re.compile(s, re.I | re.M), re.compile(g)) for k, s, g in calques}
        self.non_prose = tuple(t.get("non_prose_id_prefixes", []))
        self.target_script = re.compile(t.get("target_script", r"[ぁ-んァ-ヶ]"))
        patterns = cfg.get("placeholder_patterns", [])
        self.token = re.compile("|".join(patterns)) if patterns else None
        self.min_ratio = t.get("min_ratio", MIN_RATIO)
        self.min_gap = t.get("min_gap", MIN_GAP)
        self.min_n = t.get("min_n", MIN_N)

    def strip(self, text: str) -> str:
        return self.token.sub("", text) if self.token else text


def is_prose(row: dict, cfg: Config) -> bool:
    """会話・地の文か（名前・説明文など、語の選び方の規則が違う枠は non_prose_id_prefixes で外す）。"""
    if cfg.non_prose and row["id"].startswith(cfg.non_prose):
        return False
    return bool(cfg.target_script.search(row.get("tgt") or ""))


def side(row: dict) -> str | None:
    """"l"（原作 locked）/ "t"（機械翻訳）/ None（測らない）。"""
    if not row.get("tgt"):
        return None
    if row["status"] == "locked":
        return "l"
    return None if row["status"] in _NOT_MEASURED else "t"


def calque_rates(rows: list[dict], cfg: Config) -> dict[str, dict]:
    out: dict[str, dict] = {}
    prose = [(side(r), r) for r in rows if side(r) and is_prose(r, cfg)]
    for key, (src_re, tgt_re) in cfg.calques.items():
        n = {"t": 0, "l": 0}
        hit = {"t": 0, "l": 0}
        for s, r in prose:
            if src_re.search(r["src"]):
                n[s] += 1
                hit[s] += bool(tgt_re.search(r["tgt"]))
        out[key] = {
            "n_t": n["t"], "t": hit["t"] / n["t"] if n["t"] else 0.0,
            "n_l": n["l"], "l": hit["l"] / n["l"] if n["l"] else 0.0,
        }
    return out


def adopted(rates: dict[str, dict], cfg: Config) -> list[str]:
    """機械翻訳が原作より明らかに多く訳出している語。"""
    return [
        k for k, r in rates.items()
        if r["n_t"] >= cfg.min_n and r["n_l"] >= cfg.min_n
        and r["t"] >= cfg.min_ratio * r["l"] and r["t"] - r["l"] >= cfg.min_gap
    ]


def avoided(rates: dict[str, dict], cfg: Config) -> list[str]:
    """原作が訳さずに済ませている語（翻訳前のプロジェクトで、翻訳方針に書く候補）。"""
    return [k for k, r in rates.items() if r["n_l"] >= cfg.min_n and r["l"] <= AVOIDED_MAX]


def exemplars(rows: list[dict], key: str, cfg: Config, k: int = 4, max_len: int = 40) -> list[dict]:
    """原文にその語があるのに訳語を使っていない原作の短い行（書き直し・翻訳方針の手本）。"""
    src_re, tgt_re = cfg.calques[key]
    out = []
    for r in rows:
        if (
            side(r) == "l" and is_prose(r, cfg) and len(r["tgt"]) <= max_len
            and src_re.search(r["src"]) and not tgt_re.search(r["tgt"])
        ):
            out.append({"src": r["src"], "tgt": r["tgt"]})
            if len(out) >= k:
                break
    return out


def comma_heavy(tgt: str, cfg: Config) -> bool:
    return any(cfg.strip(line).count("、") >= _COMMA_LINE for line in tgt.split("\n"))


def teiru_heavy(tgt: str, comma: bool = False) -> bool:
    n = len(_TEIRU.findall(tgt))
    return n >= 2 or (comma and n >= 1)


def default_narration_ids(rows: list[dict], cfg: Config) -> set[str]:
    """地の文の目安（「」を含まない一定長以上の行）。話者を引けるプロジェクトは自前で渡す。"""
    return {
        r["id"] for r in rows
        if side(r) and is_prose(r, cfg) and "「" not in r["tgt"]
        and len(cfg.strip(r["tgt"])) >= NARRATION_MIN_LEN
    }


def narration_rates(rows: list[dict], narration_ids: set[str], cfg: Config) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, pred in (
        ("読点2つ以上の行", lambda r: comma_heavy(r["tgt"], cfg)),
        ("〜ている2つ以上", lambda r: teiru_heavy(r["tgt"])),
    ):
        d = out.setdefault(name, {})
        for key in ("t", "l"):
            rs = [r for r in rows if r["id"] in narration_ids and side(r) == key]
            d[f"n_{key}"] = len(rs)
            d[key] = sum(1 for r in rs if pred(r)) / len(rs) if rs else 0.0
    return out


def flag_rows(
    rows: list[dict],
    keys: list[str],
    cfg: Config,
    narration_ids: set[str] = frozenset(),
    exclude_ids: set[str] = frozenset(),
) -> dict[str, list[str]]:
    """機械翻訳の行に、採用した語・読点つなぎ・〜ているのタグを付ける。"""
    # 同じ原文・同じ訳が locked にある行は原作の言い回しそのもの。直すと原作と食い違う
    canon = {(r["src"], r["tgt"]) for r in rows if side(r) == "l"}
    tags: dict[str, list[str]] = {}
    for r in rows:
        if side(r) != "t" or r["id"] in exclude_ids or not is_prose(r, cfg):
            continue
        if (r["src"], r["tgt"]) in canon:
            continue
        t = [
            f"calque:{k}" for k in keys
            if cfg.calques[k][0].search(r["src"]) and cfg.calques[k][1].search(r["tgt"])
        ]
        if r["id"] in narration_ids:
            comma = comma_heavy(r["tgt"], cfg)
            if comma:
                t.append("comma")
            if teiru_heavy(r["tgt"], comma):
                t.append("teiru")
        if t:
            tags[r["id"]] = t
    return tags


def measure(
    rows: list[dict],
    cfg: Config,
    narration_ids: set[str] | None = None,
    exclude_ids: set[str] = frozenset(),
) -> dict:
    """測定の本体（純粋関数）。narration_ids を省くと default_narration_ids で決める。"""
    if narration_ids is None:
        narration_ids = default_narration_ids(rows, cfg)
    rates = calque_rates(rows, cfg)
    keys = adopted(rates, cfg)
    avoid = avoided(rates, cfg)
    return {
        "rates": rates,
        "adopted": keys,
        "avoided": avoid,
        "narration_rates": narration_rates(rows, narration_ids, cfg),
        "exemplars": {k: exemplars(rows, k, cfg) for k in dict.fromkeys(keys + avoid)},
        "tags": flag_rows(rows, keys, cfg, narration_ids, exclude_ids),
    }


def render_report(result: dict, cfg: Config) -> str:
    rates = result["rates"]
    lines = [
        "# 翻訳調レポート（herma scripts/translationese.py）",
        "",
        "英語原文にその語がある行だけを母数にした訳出率（会話・地の文のみ）。",
        f"採用 = 機械翻訳が原作の{cfg.min_ratio:g}倍以上・{cfg.min_gap:.0%}以上高い・母数が両側とも{cfg.min_n}行以上。",
        f"原作が避ける = 原作の率が{AVOIDED_MAX:.0%}以下・母数{cfg.min_n}行以上（翻訳方針に書く候補）。",
        "",
        "| 訳語 | 採用 | 原作が避ける | 機械翻訳 | 母数 | 原作 | 母数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, r in rates.items():
        a = "○" if k in result["adopted"] else ""
        v = "○" if k in result["avoided"] else ""
        lines.append(f"| {k} | {a} | {v} | {r['t']:.1%} | {r['n_t']} | {r['l']:.1%} | {r['n_l']} |")
    lines += ["", "## 地の文の組み立て", "", "| 指標 | 機械翻訳 | 母数 | 原作 | 母数 |", "|---|---|---|---|---|"]
    for k, r in result["narration_rates"].items():
        lines.append(f"| {k} | {r['t']:.1%} | {r['n_t']} | {r['l']:.1%} | {r['n_l']} |")
    if result["exemplars"]:
        lines += ["", "## 原作の手本（原文にその語があるのに訳していない行）", ""]
        for k, exs in result["exemplars"].items():
            lines.append(f"### {k}")
            lines += [f"- `{e['src']}` → `{e['tgt']}`" for e in exs]
    counts: dict[str, int] = {}
    for t in result["tags"].values():
        for x in t:
            counts[x] = counts.get(x, 0) + 1
    lines += ["", f"## タグ別件数（対象 {len(result['tags'])}行）", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="翻訳調を原作訳（locked）と比べて測る")
    parser.add_argument("project")
    project = Path(parser.parse_args(argv).project)
    cfg = Config(json.loads((project / "tl.config.json").read_text(encoding="utf-8")))
    rows = [r for p in sorted((project / "entries").glob("*.jsonl")) for r in entries_mod.load_jsonl(p)]
    result = measure(rows, cfg)
    qa = project / "qa"
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "translationese.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (qa / "translationese-report.md").write_text(render_report(result, cfg), encoding="utf-8")
    print(
        f"採用 {len(result['adopted'])}語 / 原作が避ける {len(result['avoided'])}語 / "
        f"タグ {len(result['tags'])}行 -> {qa / 'translationese-report.md'}"
    )
    return result


if __name__ == "__main__":
    main()
