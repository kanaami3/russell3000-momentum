#!/usr/bin/env python3
"""JPXの新規上場会社情報から直近のIPO一覧を取得する。

出典: https://www.jpx.co.jp/listing/stocks/new/
  index.html          … 当年分（上場予定を含む）
  00-archives-01.html … 前年分
  00-archives-02.html … 前々年分

**1件の銘柄が2行に分かれている。**
JPXの表は「rows-2」構造で、1レコードが必ず tr 2本で表現される。

  1行目: 上場日(rowspan2) / 会社名(rowspan2) / コード / 会社概要 / 確認書 / 仮条件 / 公募 / 売買単位
  2行目: 市場区分 / Iの部 / CG報告書 / 公募・売出価格 / 売出 / 決算短信

上場日と会社名だけが rowspan=2 なので、2行目のセル数は6本になる。1行ずつ
読むと市場区分や公開価格が別レコードのものとして混ざる。必ず2行ペアで読む。

**上場予定の銘柄は価格が「-」で入っている。**
仮条件も公開価格も決定前は "-"。欠損と区別する必要はないが、数値として
扱えないので None にする。「未定」と「取得失敗」を同じ None にすると
画面で嘘をつくので、listed（上場済みか）フラグで区別できるようにしておく。

**コードは4桁数字ではない。**
2024年以降の新規上場は「648A」のように英字を含む（コード枯渇への対応）。
int でパースしてはいけない。yfinance に渡すときは "648A.T" になる。

**会社名に余計な文字が混ざる。**
「（株）KOMPEITO 代表者インタビュー」のようにリンクのテキストが連結される。
末尾の注記（代表者インタビュー、* など）を落とす。

Output: data/ipo_jp.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "ipo_jp.json"

BASE = "https://www.jpx.co.jp/listing/stocks/new/"
PAGES = ["index.html", "00-archives-01.html", "00-archives-02.html"]

JST = timezone(timedelta(hours=9))
YEARS_BACK = int(os.getenv("IPO_YEARS_BACK", "2"))

UA = "russell3000-momentum/1.0 (+https://github.com/kanaami3/russell3000-momentum)"
TIMEOUT = 30

# 会社名の末尾に付く注記。リンクテキストがそのまま連結されてくる。
NAME_NOISE = re.compile(r"(代表者インタビュー|新規上場申請のための[^\s]*|PDF|Excel|\*+|[（(]注\d+[)）])\s*$")


def clean_name(s: str) -> str:
    """末尾の注記を落とす。複数付くことがあるので変化しなくなるまで繰り返す。"""
    prev = None
    while prev != s:
        prev = s
        s = NAME_NOISE.sub("", s).strip()
    return s
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")
# 銘柄コードは4桁。旧来は数字4つ（5871）、2024年以降の新規上場は
# 数字3つ＋英字1つ（648A）。4桁数字だけを想定すると新規上場が全滅する。
CODE_RE = re.compile(r"^([0-9]{3}[0-9A-Z])")


def _text(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def _num(s: str) -> float | None:
    """「1,060」「4,139.3(OA628.2)」→ 数値。「-」「1,020～1,060」→ None。

    OA（オーバーアロットメント）は括弧の中なので、括弧より前だけを見る。
    仮条件のような範囲表記は単一の数値にできないので None を返す。
    """
    s = (s or "").split("(")[0].strip().replace(",", "")
    if not s or s in {"-", "‐", "―", "未定"}:
        return None
    if "～" in s or "〜" in s or "-" in s[1:]:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _range(s: str) -> tuple[float | None, float | None]:
    """仮条件「1,020～1,060」→ (1020.0, 1060.0)。"""
    s = (s or "").replace(",", "").replace("〜", "～")
    if "～" not in s:
        return (None, None)
    lo, _, hi = s.partition("～")
    try:
        return (float(lo), float(hi))
    except ValueError:
        return (None, None)


def parse_table(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for t in soup.find_all("table"):
        if "上場日" in t.get_text():
            table = t
            break
    if table is None:
        return []

    rows = table.find_all("tr")
    out: list[dict] = []
    skipped = 0

    i = 0
    while i + 1 < len(rows):
        a = rows[i].find_all(["td", "th"])
        b = rows[i + 1].find_all(["td", "th"])

        # ヘッダ行（th のみ）は飛ばす。2行ヘッダなので2本まとめて進める。
        if rows[i].find("td") is None:
            i += 2
            continue

        if len(a) < 8 or len(b) < 6:
            # 構造が変わった可能性。黙って捨てず件数を数えて後で警告する。
            skipped += 1
            i += 1
            continue

        m = DATE_RE.search(_text(a[0]))
        cm = CODE_RE.match(_text(a[2]))
        if not m or not cm:
            skipped += 1
            i += 2
            continue

        listing_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        approved = DATE_RE.findall(_text(a[0]))
        approval_date = None
        if len(approved) >= 2:
            y, mo, d = approved[1]
            approval_date = f"{y}-{mo}-{d}"

        cond_lo, cond_hi = _range(_text(a[5]))
        raw_name = _text(a[1])
        # JPXは持株会社化・テクニカル上場など「実質的な新規公開ではない」
        # 銘柄に（※）を付けている。公募をせず既存株主がそのまま移るので、
        # 初値や公開価格の意味が普通のIPOと違う。画面で区別できるよう
        # 印を残し、名前からは落とす。
        technical = "※" in raw_name
        name = clean_name(raw_name.replace("（※）", "").replace("(※)", ""))

        out.append({
            "code": cm.group(1),
            "name": name,
            "technical": technical,
            "listing_date": listing_date,
            "approval_date": approval_date,
            "market": _text(b[0]) or None,
            "band_low": cond_lo,
            "band_high": cond_hi,
            "offer_price": _num(_text(b[3])),       # 公募・売出価格（決定後）
            "public_shares_k": _num(_text(a[6])),   # 公募（千株）
            "secondary_shares_k": _num(_text(b[4])),  # 売出（千株）
            "trading_unit": _num(_text(a[7])),
        })
        i += 2

    if skipped:
        print(f"  警告: 解釈できない行が {skipped} 件ありました", file=sys.stderr)
    return out


def fetch_page(page: str) -> list[dict]:
    url = BASE + page
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    rows = parse_table(r.text)
    print(f"{page}: {len(rows)} 件", file=sys.stderr)
    return rows


def main() -> int:
    now = datetime.now(JST)
    cutoff = (now - timedelta(days=365 * YEARS_BACK + 1)).date().isoformat()

    records: dict[str, dict] = {}
    failures = 0
    for page in PAGES:
        try:
            for row in fetch_page(page):
                # 同一銘柄が複数ページに出ることはないが、念のため上場日の
                # 新しい方を残す（承認取消→再申請で重複しうる）。
                prev = records.get(row["code"])
                if prev is None or row["listing_date"] > prev["listing_date"]:
                    records[row["code"]] = row
        except Exception as e:
            failures += 1
            print(f"{page} の取得に失敗: {e}", file=sys.stderr)

    if not records:
        # 全滅したら既存ファイルを壊さない。空で上書きすると画面が消える。
        print("1件も取得できませんでした。既存ファイルを維持します。", file=sys.stderr)
        return 1

    rows = [r for r in records.values() if r["listing_date"] >= cutoff]
    rows.sort(key=lambda r: (r["listing_date"], r["code"]), reverse=True)

    today = now.date().isoformat()
    for r in rows:
        r["listed"] = r["listing_date"] <= today

    payload = {
        "asof": today,
        "generated_at_jst": now.isoformat(timespec="seconds"),
        "source": BASE,
        "cutoff": cutoff,
        "years_back": YEARS_BACK,
        "pages_failed": failures,
        "count": len(rows),
        "upcoming": sum(1 for r in rows if not r["listed"]),
        "ipos": rows,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    print(f"{len(rows)} 件を書き出しました（上場予定 {payload['upcoming']} 件）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
