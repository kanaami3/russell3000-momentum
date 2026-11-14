"""直近の増配・配当予想の増額修正を TDnet の適時開示一覧から拾う。

TDnet の日次一覧 (https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html,
1ページ100件、002, 003… と続く) を直近 LOOKBACK_DAYS 日分読み、
タイトルに「増配」「記念配当」「配当予想の上方修正」などを含み、
「減配」「無配」を含まない開示を data/dividend_hikes_jp.json に追記する。

TDnet が取れない日があっても、既存のイベントは残すだけで失敗にはしない
(四季報×決算増配ピックは、手元にあるイベントだけで作り直せるため)。
イベントは KEEP_DAYS 日より古いものを捨てる。
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "data" / "dividend_hikes_jp.json"
JST = timezone(timedelta(hours=9))
BASE = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{ymd}.html"
LOOKBACK_DAYS = 7
KEEP_DAYS = 200
MAX_PAGES = 15

HIKE = re.compile(r"増配|記念配当|特別配当|配当予想の上方修正|配当予想の増額|復配")
NOT_HIKE = re.compile(r"減配|無配|取りやめ|取り止め|見送り|訂正")

ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r'<td[^>]*class="[^"]*kj(Code|Name|Title)[^"]*"[^>]*>(.*?)</td>', re.S)
TAG = re.compile(r"<[^>]+>")


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", TAG.sub("", s)).strip()


def fetch_day(day: datetime) -> list[dict] | None:
    ymd = day.strftime("%Y%m%d")
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = BASE.format(page=page, ymd=ymd)
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as e:
            print(f"  {ymd} p{page}: {e}")
            return out or None
        if r.status_code == 404:
            break
        if r.status_code != 200:
            print(f"  {ymd} p{page}: HTTP {r.status_code}")
            return out or None
        r.encoding = r.apparent_encoding or "utf-8"
        rows = 0
        for tr in ROW.findall(r.text):
            cells = {k: _text(v) for k, v in CELL.findall(tr)}
            if "Code" not in cells or "Title" not in cells:
                continue
            rows += 1
            title = cells["Title"]
            if not HIKE.search(title) or NOT_HIKE.search(title):
                continue
            code = cells["Code"][:4]  # TDnet は 5桁 (末尾0)
            out.append({
                "code": code,
                "date": day.strftime("%Y-%m-%d"),
                "detail": title,
                "earnings_up": bool(re.search(r"業績予想.*修正|上方修正", title)),
                "source": "TDnet",
            })
        if rows < 100:
            break
        time.sleep(0.5)
    return out


def main() -> int:
    data = {"events": []}
    if OUT.exists():
        data = json.loads(OUT.read_text(encoding="utf-8"))
    events = data.get("events", [])
    have = {(e["code"], e["date"]) for e in events}

    today = datetime.now(JST)
    added = 0
    ok_days = 0
    for i in range(LOOKBACK_DAYS):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        got = fetch_day(day)
        if got is None:
            continue
        ok_days += 1
        for e in got:
            if (e["code"], e["date"]) in have:
                continue
            have.add((e["code"], e["date"]))
            events.append(e)
            added += 1

    cutoff = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    events = sorted((e for e in events if e["date"] >= cutoff), key=lambda e: (e["date"], e["code"]))
    data["events"] = events
    data["updated_at"] = today.strftime("%Y-%m-%d")
    data["last_fetch"] = {"ok_days": ok_days, "added": added}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dividend hikes: +{added} (TDnet ok days {ok_days}/{LOOKBACK_DAYS}), total {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
