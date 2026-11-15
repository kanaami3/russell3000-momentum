"""直近の増配・配当予想の増額修正を TDnet の適時開示一覧から拾う。

TDnet の日次一覧 (https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html,
1ページ100件、002, 003… と続く) を直近 LOOKBACK_DAYS 日分読み、
タイトルに「増配」「記念配当」「配当予想の上方修正」などを含み、
「減配」「無配」を含まない開示を data/dividend_hikes_jp.json に追記する。

TDnet が取れない日があっても、既存のイベントは残すだけで失敗にはしない
(四季報×決算増配ピックは、手元にあるイベントだけで作り直せるため)。
イベントは KEEP_DAYS 日より古いものを捨てる。

タイトルだけでは「（増配）」と書かない会社を取りこぼすので、決算短信と
配当予想の修正には XBRL(サマリーの1株当たり配当)を読んで中身で判定する。
  - 決算短信: 今期(本決算なら来期)の年間配当予想 > 前期の年間配当実績 → 増配予想
  - 配当予想の修正: 今回の年間予想 > 前回の年間予想 → 増額修正
読み終えた XBRL の zip 名は xbrl_seen に残し、同じ開示を何度も読まない。
"""

from __future__ import annotations

import json
import re
import sys
import io
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "data" / "dividend_hikes_jp.json"
JST = timezone(timedelta(hours=9))
BASE_DIR = "https://www.release.tdnet.info/inbs/"
BASE = BASE_DIR + "I_list_{page:03d}_{ymd}.html"
LOOKBACK_DAYS = 7
# TDnet は約1か月分しか残らない。まだ TDnet 由来のイベントが無い(初回)ときは
# 1か月さかのぼって、手動収集で漏れた日(10/24, 10/31, 11/13 など)を埋める。
BACKFILL_DAYS = 31
KEEP_DAYS = 200
MAX_PAGES = 15
XBRL_WORKERS = 4
# XBRL を読むのは決算短信と配当関係の開示だけ(数が多いので)
XBRL_TITLE = re.compile(r"決算短信|配当")
XBRL_SKIP = re.compile(r"訂正|REIT|ＲＥＩＴ|投資法人")

HIKE = re.compile(r"増配|記念配当|特別配当|配当予想の上方修正|配当予想の増額|復配")
NOT_HIKE = re.compile(r"減配|無配|取りやめ|取り止め|見送り|訂正")

ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r'<td[^>]*class="[^"]*kj(Code|Name|Title)[^"]*"[^>]*>(.*?)</td>', re.S)
TAG = re.compile(r"<[^>]+>")
ZIP = re.compile(r'href="([^"]+\.zip)"')
IXNUM = re.compile(r"<ix:nonFraction\b([^>]*?)(?:/>|>(.*?)</ix:nonFraction>)", re.S)
ATTR = re.compile(r'([\w:]+)="([^"]*)"')


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", TAG.sub("", s)).strip()


def _dps_facts(html: str) -> dict[str, float]:
    """サマリー iXBRL から 1株当たり配当(DividendPerShare)を {contextRef: 値} で返す。"""
    out: dict[str, float] = {}
    for attrs, body in IXNUM.findall(html):
        a = dict(ATTR.findall(attrs))
        if not a.get("name", "").endswith(":DividendPerShare"):
            continue
        if a.get("xsi:nil") == "true" or not body:
            continue
        txt = TAG.sub("", body).replace(",", "").strip()
        try:
            v = float(txt)
        except ValueError:
            continue
        v *= 10 ** int(a.get("scale", "0") or 0)
        if a.get("sign") == "-":
            v = -v
        out[a.get("contextRef", "")] = v
    return out


def _pick(facts: dict[str, float], *need: str, avoid: tuple[str, ...] = ()) -> float | None:
    """contextRef に need をすべて含み avoid を含まない値。レンジ予想は下限を優先。"""
    cands = [(c, v) for c, v in facts.items()
             if all(n in c for n in need) and not any(x in c for x in avoid)]
    if not cands:
        return None
    for c, v in cands:
        if "Lower" in c:
            return v
    return cands[0][1]


def judge_xbrl(facts: dict[str, float]) -> dict | None:
    """配当の増額(予想の上方修正、または前期比の増配予想)なら説明を返す。"""
    # 配当予想の修正(rvdf / rvfc): Previous=前回予想, Current=今回予想
    prev = _pick(facts, "CurrentYearDuration", "AnnualMember", "PreviousMember")
    cur = _pick(facts, "CurrentYearDuration", "AnnualMember", "CurrentMember", avoid=("Result",))
    if prev is not None and cur is not None:
        if cur > prev and prev >= 0:
            last = _pick(facts, "PriorYearDuration", "AnnualMember", "Result")
            tail = f"、前期{last:g}円" if last is not None else ""
            return {"kind": "revision", "detail": f"年間配当予想を{prev:g}円→{cur:g}円に増額修正{tail}"}
        return None
    # 決算短信: 本決算なら来期予想 vs 今期実績、四半期なら今期予想 vs 前期実績
    nxt = _pick(facts, "NextYearDuration", "AnnualMember", avoid=("Result",))
    if nxt is not None:
        base = _pick(facts, "CurrentYearDuration", "AnnualMember", "Result")
        label = "来期"
    else:
        nxt = _pick(facts, "CurrentYearDuration", "AnnualMember", "Forecast")
        base = _pick(facts, "PriorYearDuration", "AnnualMember", "Result")
        label = "今期"
    if nxt is None or base is None or base <= 0 or nxt <= base:
        return None
    return {"kind": "tanshin", "detail": f"決算短信で{label}の年間配当予想{nxt:g}円(前期{base:g}円から増配)"}


def fetch_xbrl(name: str) -> dict[str, float] | None:
    try:
        r = requests.get(BASE_DIR + name, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        return None
    facts: dict[str, float] = {}
    for n in zf.namelist():
        # 決算短信はサマリー(Summary/ 配下)、配当予想の修正は zip 直下の ixbrl
        if not n.endswith("-ixbrl.htm") or "/Attachment/" in n:
            continue
        facts.update(_dps_facts(zf.read(n).decode("utf-8", "ignore")))
    return facts


def fetch_day(day: datetime, xbrl_seen: set[str] | None = None) -> list[dict] | None:
    ymd = day.strftime("%Y%m%d")
    out: list[dict] = []
    xbrl_jobs: list[tuple[str, str, str]] = []
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
            code = cells["Code"][:4]  # TDnet は 5桁 (末尾0)
            z = ZIP.search(tr)
            if (xbrl_seen is not None and z and XBRL_TITLE.search(title)
                    and not XBRL_SKIP.search(title) and z.group(1) not in xbrl_seen):
                xbrl_jobs.append((code, title, z.group(1)))
            # 「（増配）」と書かない会社も多い。業績の上方修正と同時の
            # 「配当予想の修正」は、ほぼ増額なので拾う。
            hike = HIKE.search(title) or ("配当予想の修正" in title and "上方修正" in title)
            if not hike or NOT_HIKE.search(title):
                continue
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
    if xbrl_jobs:
        titled = {e["code"] for e in out}
        with ThreadPoolExecutor(XBRL_WORKERS) as ex:
            results = list(ex.map(lambda j: fetch_xbrl(j[2]), xbrl_jobs))
        n_hit = 0
        for (code, title, name), facts in zip(xbrl_jobs, results):
            if facts is None:
                continue  # 取れなかった zip は次回また読む
            xbrl_seen.add(name)
            j = judge_xbrl(facts)
            if not j or code in titled:
                continue
            titled.add(code)
            n_hit += 1
            out.append({
                "code": code,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"{j['detail']}（{title}）",
                "earnings_up": bool(re.search(r"上方修正", title)),
                "source": "TDnet XBRL",
                "kind": j["kind"],
            })
        print(f"  {ymd}: XBRL {len(xbrl_jobs)} 件を確認、増配 {n_hit} 件")
    return out


def main() -> int:
    data = {"events": []}
    if OUT.exists():
        data = json.loads(OUT.read_text(encoding="utf-8"))
    events = data.get("events", [])
    have = {(e["code"], e["date"]) for e in events}

    xbrl_seen = set(data.get("xbrl_seen", []))
    first_xbrl = "xbrl_seen" not in data
    today = datetime.now(JST)
    lookback = LOOKBACK_DAYS if any(e.get("source") == "TDnet" for e in events) else BACKFILL_DAYS
    if first_xbrl:
        lookback = BACKFILL_DAYS  # XBRL 判定を入れた最初の回は1か月分を読み直す
    added = 0
    ok_days = 0
    for i in range(lookback):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        got = fetch_day(day, xbrl_seen)
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
    # zip 名は「0812 + 日付時刻 + 連番.zip」。先頭4文字を除いた日付順で古いものから捨てる
    data["xbrl_seen"] = sorted(xbrl_seen, key=lambda n: n[4:])[-20000:]
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dividend hikes: +{added} (TDnet ok days {ok_days}, lookback {lookback}d), total {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
