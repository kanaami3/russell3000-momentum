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
# 会社予想。四半期ごとに開示し直されるので、1年より古いものは捨てる
FORECAST_OUT = REPO_ROOT / "data" / "dividend_forecast_jp.json"
FORECAST_KEEP_DAYS = 400
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


# 1株当たり利益の要素名は会計基準で違う(日本基準 NetIncomePerShare、IFRS は
# BasicEarningsPerShareIFRS、米国基準は …US)。希薄化後は使わない。
EPS_NAME = re.compile(r":(NetIncomePerShare|BasicEarningsPerShare|EarningsPerShare)(IFRS|US|JMIS)?$")
# 予想の前期比(純利益)。scale=-2 なので読むと比率(0.266 = +26.6%)になる。
CHG_NAME = re.compile(r":ChangeIn(ProfitAttributableToOwnersOfParent|NetIncome|Profit)(IFRS|US|JMIS)?$")


def _facts(html: str) -> dict[str, dict[str, float]]:
    """サマリー iXBRL から {"dps"|"eps"|"chg": {contextRef: 値}} を返す。"""
    # dps_nil: 値が空(「未定」など)の配当の欄。予想が未定かどうかの判定に使う
    out: dict[str, dict] = {"dps": {}, "eps": {}, "chg": {}, "dps_nil": {}}
    for attrs, body in IXNUM.findall(html):
        a = dict(ATTR.findall(attrs))
        name = a.get("name", "")
        if name.endswith(":DividendPerShare"):
            kind = "dps"
        elif EPS_NAME.search(name):
            kind = "eps"
        elif CHG_NAME.search(name):
            kind = "chg"
        else:
            continue
        if a.get("xsi:nil") == "true" or not body:
            if kind == "dps":
                out["dps_nil"][a.get("contextRef", "")] = None
            continue
        txt = TAG.sub("", body).replace(",", "").strip()
        try:
            v = float(txt)
        except ValueError:
            continue
        v *= 10 ** int(a.get("scale", "0") or 0)
        if a.get("sign") == "-":
            v = -v
        out[kind][a.get("contextRef", "")] = v
    return out


def _dps_facts(html: str) -> dict[str, float]:
    """サマリー iXBRL から 1株当たり配当(DividendPerShare)を {contextRef: 値} で返す。"""
    return _facts(html)["dps"]


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


def _pick_fc(facts: dict[str, float], period: str, *need: str) -> float | None:
    """予想値。点予想(Forecast)を優先し、レンジ予想なら下限(Lower)を採る。"""
    v = _pick(facts, period, *need, "Forecast")
    if v is None:
        v = _pick(facts, period, *need, "Lower")
    return v


def _period_ctx(facts: dict[str, float], period: str, *need: str) -> bool:
    """その期の予想の欄があるか(値が「未定」で空でも、文脈があれば欄はある)。"""
    return any(period in c and all(n in c for n in need) for c in facts)


def extract_forecast(all_facts: dict[str, dict[str, float]]) -> dict | None:
    """会社が開示した「今の」年間配当予想と、その前提の1株利益予想を取り出す。

    配当スクリーナーの利回り・配当性向は、yfinance の利回り(過去12か月の実績や
    古い配当がまざる)ではなく、ここで取った会社予想で計算し直す。
      - 配当予想の修正(Previous/Current がある): 今回の年間予想
      - 本決算の短信(NextYear がある): 来期の年間予想 / 比較は今期の実績
      - 四半期の短信: 今期の年間予想 / 比較は前期の実績
    年間予想が「未定」で空のときは dps=None・undecided=True を返す
    (古い配当で利回りを出し続けないため)。予想の欄が無い開示は None。
    """
    dps, eps, chg = all_facts.get("dps", {}), all_facts.get("eps", {}), all_facts.get("chg", {})
    slots = {**all_facts.get("dps_nil", {}), **dps}   # 値の有無を問わず、欄があるか
    out: dict = {}
    prev = _pick(dps, "CurrentYearDuration", "AnnualMember", "PreviousMember")
    cur = _pick(dps, "CurrentYearDuration", "AnnualMember", "CurrentMember", avoid=("Result",))
    if prev is not None or cur is not None:
        out["kind"] = "revision"
        out["dps"] = cur
        out["prior"] = _pick(dps, "PriorYearDuration", "AnnualMember", "Result")
        period = "CurrentYearDuration"
    elif _period_ctx(slots, "NextYearDuration", "AnnualMember"):
        out["kind"] = "tanshin_full"
        out["dps"] = _pick_fc(dps, "NextYearDuration", "AnnualMember")
        out["prior"] = _pick(dps, "CurrentYearDuration", "AnnualMember", "Result")
        period = "NextYearDuration"
    elif _period_ctx(slots, "CurrentYearDuration", "AnnualMember", "Forecast") or \
            _period_ctx(slots, "CurrentYearDuration", "AnnualMember", "Lower"):
        out["kind"] = "tanshin"
        out["dps"] = _pick_fc(dps, "CurrentYearDuration", "AnnualMember")
        out["prior"] = _pick(dps, "PriorYearDuration", "AnnualMember", "Result")
        period = "CurrentYearDuration"
    else:
        return None
    # 1株利益・純利益の前期比は連結を優先(個別しか出さない会社もある)
    for need in (("ConsolidatedMember",), ("NonConsolidatedMember",), ()):
        avoid = ("NonConsolidated",) if need == ("ConsolidatedMember",) else ()
        e = _pick_fc_avoid(eps, period, need, avoid)
        if e is not None:
            out["eps"] = e
            c = _pick_fc_avoid(chg, period, need, avoid)
            if c is not None:
                out["profit_chg"] = round(c, 4)
            break
    out["undecided"] = out["dps"] is None
    return out


def _pick_fc_avoid(facts, period, need, avoid):
    v = _pick(facts, period, *need, "Forecast", avoid=avoid)
    if v is None:
        v = _pick(facts, period, *need, "Lower", avoid=avoid)
    return v


def fetch_xbrl(name: str) -> dict[str, dict[str, float]] | None:
    """zip を読み、{"dps"|"eps"|"chg": {contextRef: 値}} を返す。取れなければ None。"""
    try:
        r = requests.get(BASE_DIR + name, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        return None
    facts: dict[str, dict] = {"dps": {}, "eps": {}, "chg": {}, "dps_nil": {}}
    for n in zf.namelist():
        # 決算短信はサマリー(Summary/ 配下)、配当予想の修正は zip 直下の ixbrl
        if not n.endswith("-ixbrl.htm") or "/Attachment/" in n:
            continue
        for k, v in _facts(zf.read(n).decode("utf-8", "ignore")).items():
            facts[k].update(v)
    return facts


def fetch_day(day: datetime, xbrl_seen: set[str] | None = None,
              forecasts: dict | None = None, reread: bool = False) -> list[dict] | None:
    """その日の増配イベントを返す。forecasts を渡すと、読んだ XBRL の会社予想を書き足す。
    reread=True なら既読(xbrl_seen)の zip も読み直す(会社予想の初回取り込み用)。"""
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
                    and not XBRL_SKIP.search(title) and (reread or z.group(1) not in xbrl_seen)):
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
            if forecasts is not None:
                fc = extract_forecast(facts)
                if fc is not None:
                    fc.update({"date": day.strftime("%Y-%m-%d"), "zip": name, "title": title[:80]})
                    old = forecasts.get(code)
                    # 同じ銘柄は新しい開示で上書き(zip 名の先頭4文字の後ろが日時)
                    if old is None or (old.get("date"), old.get("zip", "")[4:]) <= (fc["date"], name[4:]):
                        # 配当予想だけの修正には利益予想が無い。直前の短信の利益予想を引き継ぐ
                        if old and fc["kind"] == "revision":
                            for k in ("eps", "profit_chg"):
                                if k not in fc and k in old:
                                    fc[k] = old[k]
                        forecasts[code] = fc
            j = judge_xbrl(facts["dps"])
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
    fc_data = {"forecasts": {}}
    if FORECAST_OUT.exists():
        fc_data = json.loads(FORECAST_OUT.read_text(encoding="utf-8"))
    forecasts = fc_data.get("forecasts", {})
    # 会社予想を取り始めた最初の回は、既読の zip も1か月分読み直して埋める
    reread = not forecasts
    today = datetime.now(JST)
    lookback = LOOKBACK_DAYS if any(e.get("source") == "TDnet" for e in events) else BACKFILL_DAYS
    if first_xbrl or reread:
        lookback = BACKFILL_DAYS  # XBRL 判定・会社予想を入れた最初の回は1か月分を読み直す
    added = 0
    ok_days = 0
    for i in range(lookback):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        got = fetch_day(day, xbrl_seen, forecasts, reread)
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

    fc_cut = (today - timedelta(days=FORECAST_KEEP_DAYS)).strftime("%Y-%m-%d")
    forecasts = {c: f for c, f in sorted(forecasts.items()) if f.get("date", "") >= fc_cut}
    FORECAST_OUT.write_text(json.dumps({
        "note": "TDnet の決算短信・配当予想の修正(XBRLサマリー)から取った、銘柄ごとの最新の会社予想。"
                "dps=年間配当予想(円, 未定なら null) / prior=比較対象の前期(本決算なら今期)の年間配当実績 / "
                "eps=1株利益予想 / profit_chg=純利益予想の前期比(比率)。batch/fetch_dividend_hikes.py が毎日更新。",
        "updated_at": today.strftime("%Y-%m-%d"),
        "forecasts": forecasts,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"company forecasts: {len(forecasts)} 銘柄")
    print(f"dividend hikes: +{added} (TDnet ok days {ok_days}, lookback {lookback}d), total {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
