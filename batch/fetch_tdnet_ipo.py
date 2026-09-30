#!/usr/bin/env python3
"""TDnet（適時開示）から、IPO銘柄の決算関連の開示を拾って貯める。

出典: https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html

**TDnetは31日分しか残らない。**
過去に遡って取り直すことができないので、毎日拾って自前で貯めるしかない。
逆に言えば、走り始めた日より前の決算は取れない。画面では「いつから
集めているか」を出して、空欄を「決算が無い」と誤読させないようにする。

**銘柄コードは5桁で来る。**
TDnetは4桁コードの末尾に0を足した5桁で表示する（7485 → 74850）。
2024年以降の英字入りコードも同じで、190A → 190A0。先頭4文字を取る。

**1日が複数ページに分かれる。**
1ページ50件で、決算シーズンは1日400件を超える。「全N件」を読んで
必要なページ数だけ取りに行く。ここを1ページで済ませると、決算集中日の
夕方に出た短信を取りこぼす。

Input:  data/ipo_jp.json（対象銘柄の絞り込みに使う）
Output: data/tdnet_ipo_jp.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
IPO_LIST = REPO_ROOT / "data" / "ipo_jp.json"
OUT_PATH = REPO_ROOT / "data" / "tdnet_ipo_jp.json"

BASE = "https://www.release.tdnet.info/inbs/"
JST = timezone(timedelta(hours=9))

UA = "russell3000-momentum/1.0 (+https://github.com/kanaami3/russell3000-momentum)"
TIMEOUT = 30
SLEEP = float(os.getenv("TDNET_SLEEP", "0.5"))

# 何日ぶん遡るか。祝日や実行漏れを拾い直すため既定で3日。
BACKFILL_DAYS = int(os.getenv("TDNET_BACKFILL_DAYS", "3"))
PER_PAGE = 50
MAX_PAGES = 30          # 1日1500件を超えることはない
KEEP_DAYS = int(os.getenv("TDNET_KEEP_DAYS", "800"))

TOTAL_RE = re.compile(r"全\s*(\d+)\s*件")

# 拾う開示の種類。ここに無いものは保存しない（人事異動や自己株買いまで
# 貯めると、決算を探すときに埋もれる）。
KINDS = [
    ("決算短信", re.compile(r"決算短信")),
    ("業績予想の修正", re.compile(r"業績予想.*修正|業績予想の修正")),
    ("配当予想の修正", re.compile(r"配当予想.*修正")),
    ("月次", re.compile(r"月次")),
]


def classify(title: str) -> str | None:
    for name, pat in KINDS:
        if pat.search(title):
            return name
    return None


def _get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def parse_page(html: str) -> tuple[list[dict], int]:
    """1ページ分の開示を返す。あわせて「全N件」も返す。"""
    soup = BeautifulSoup(html, "html.parser")

    total = 0
    m = TOTAL_RE.search(soup.get_text(" ", strip=True))
    if m:
        total = int(m.group(1))

    out = []
    for tr in soup.find_all("tr"):
        code = tr.find("td", class_=re.compile(r"kjCode"))
        title = tr.find("td", class_=re.compile(r"kjTitle"))
        if not code or not title:
            continue
        t = title.get_text(" ", strip=True)
        kind = classify(t)
        if not kind:
            continue

        tm = tr.find("td", class_=re.compile(r"kjTime"))
        name = tr.find("td", class_=re.compile(r"kjName"))
        xbrl = tr.find("td", class_=re.compile(r"kjXbrl"))
        a = title.find("a")
        z = xbrl.find("a") if xbrl else None

        code5 = code.get_text(strip=True)
        out.append({
            "code": code5[:4],
            "code5": code5,
            "name": name.get_text(" ", strip=True) if name else "",
            "time": tm.get_text(strip=True) if tm else "",
            "title": t,
            "kind": kind,
            "pdf": (a.get("href") or "") if a else "",
            "xbrl": (z.get("href") or "") if z else "",
        })
    return out, total


def fetch_day(day: str) -> list[dict]:
    """YYYYMMDD の開示をページをまたいで全部取る。"""
    rows, total = [], None
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE}I_list_{page:03d}_{day}.html"
        try:
            html = _get(url)
        except Exception as e:
            if page == 1:
                print(f"  {day}: 取得失敗 {e}", file=sys.stderr)
            break
        got, tot = parse_page(html)
        if total is None:
            total = tot
        rows.extend(got)
        if total is None or page * PER_PAGE >= total:
            break
        time.sleep(SLEEP)
    print(f"  {day}: 全{total or 0}件中 決算関連 {len(rows)}件", file=sys.stderr)
    return rows


def main() -> int:
    if not IPO_LIST.exists():
        print(f"{IPO_LIST} がありません。", file=sys.stderr)
        return 0
    codes = {str(r["code"]) for r in
             json.loads(IPO_LIST.read_text(encoding="utf-8")).get("ipos", [])}
    if not codes:
        print("対象銘柄がありません。", file=sys.stderr)
        return 0

    store = {"first_collected": None, "disclosures": []}
    if OUT_PATH.exists():
        try:
            store = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            store.setdefault("disclosures", [])
        except (json.JSONDecodeError, OSError):
            # 壊れていたら止める。空で上書きすると貯めた履歴が消える。
            print("既存ファイルが読めません。中断します。", file=sys.stderr)
            return 1

    now = datetime.now(JST)
    # pdf のファイル名が開示ごとに一意なので、それを鍵にする。
    seen = {d.get("pdf") or f"{d['date']}_{d['code']}_{d['title']}"
            for d in store["disclosures"]}

    added = 0
    for i in range(BACKFILL_DAYS):
        d = now.date() - timedelta(days=i)
        if d.weekday() >= 5:      # 土日は開示が無い
            continue
        day = d.strftime("%Y%m%d")
        for row in fetch_day(day):
            if row["code"] not in codes:
                continue
            key = row["pdf"] or f"{d.isoformat()}_{row['code']}_{row['title']}"
            if key in seen:
                continue
            seen.add(key)
            store["disclosures"].append({"date": d.isoformat(), **row})
            added += 1
        time.sleep(SLEEP)

    cutoff = (now.date() - timedelta(days=KEEP_DAYS)).isoformat()
    store["disclosures"] = [d for d in store["disclosures"] if d["date"] >= cutoff]
    store["disclosures"].sort(key=lambda d: (d["date"], d.get("time", "")), reverse=True)

    if not store.get("first_collected"):
        store["first_collected"] = now.date().isoformat()
    store["updated_at"] = now.isoformat(timespec="seconds")
    store["count"] = len(store["disclosures"])
    store["source"] = BASE

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    print(f"新規 {added} 件 / 累計 {store['count']} 件"
          f"（収集開始 {store['first_collected']}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
