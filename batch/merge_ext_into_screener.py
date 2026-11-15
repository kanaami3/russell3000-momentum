"""スタンダード・グロースの週次CSVだけで、いまの配当スクリーナーを更新する。

日次ジョブ以外(週次・手動)でプライムの指標を取り直さずに済ませるためのもの。
web/data/dividend_screener.json のうちスタンダード・グロースの行だけを、
data/value_data_jp_ext.csv から作り直した行で置き換える。通信なし。
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

import build_dividend_screener as b

EXT_MARKET = "スタンダード・グロース"


def main() -> int:
    if not b.EXT_CSV.exists() or not b.OUTPUT_PATH.exists():
        print("ext CSV か screener が無いのでスキップ")
        return 0
    cur = json.loads(b.OUTPUT_PATH.read_text(encoding="utf-8"))
    prime = [s for s in cur["stocks"] if s.get("market", "プライム") != EXT_MARKET]
    if len(prime) < 500:
        print(f"ERROR: いまのスクリーナーにプライムが {len(prime)} 銘柄しかありません。中止します。", file=sys.stderr)
        return 1

    with open(b.EXT_CSV, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("current_price")]
    with tempfile.TemporaryDirectory() as d:
        tmp_csv = Path(d) / "ext.csv"
        with open(tmp_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        b.INPUT_CSV = tmp_csv
        b.EXT_CSV = Path(d) / "none.csv"
        b.HISTORY_CSV = b.EXT_HISTORY_CSV
        real_out = b.OUTPUT_PATH
        b.OUTPUT_PATH = Path(d) / "ext.json"
        if b.main() != 0:
            return 1
        ext = json.loads(b.OUTPUT_PATH.read_text(encoding="utf-8"))

    have = {s["code"] for s in prime}
    add = []
    for s in ext["stocks"]:
        if s["code"] in have:
            continue
        s["market"] = EXT_MARKET
        add.append(s)
    for s in prime:
        s.setdefault("market", "プライム")
    cur["stocks"] = prime + add
    cur["universe_size"] = len(cur["stocks"])
    real_out.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    print(f"merged: プライム {len(prime)} + スタンダード・グロース {len(add)} = {len(cur['stocks'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
