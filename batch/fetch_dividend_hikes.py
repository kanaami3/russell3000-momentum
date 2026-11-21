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

**記念配当・特別配当は増配に数えない。**
創立◯周年などの一過性の上乗せは翌期に剝落する。三菱化工機(6331)は
「配当予想（期末配当及び記念配当）」のタイトルで増配扱いになり、利回りにも
記念分が入っているように見えた。そこで
  - タイトルに「記念配当」「特別配当」を含む開示は special として残し、増配にしない
  - XBRL の配当予想は、本文の注記(「期末配当の内訳 普通配当 69円 記念配当 6円」など)
    から記念・特別配当の額を読み、差し引いた普通配当どうしで比べる
  - 記念・特別配当に触れているのに額が読めないときは、増配と断定しない
  - 読めた記念・特別配当の額は specials に貯め、dividend_streak.load_history が
    配当履歴から差し引く(利回り・配当性向・連続増配も普通配当ベースになる)
記念分を除いても普通配当が増えていれば、その開示は増配として残る
(6331 は普通配当が前期115円→今期129円なので増配のまま)。
"""

from __future__ import annotations

import json
import re
import sys
import io
import unicodedata
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
# 一過性の配当。タイトルに含まれていれば、普通配当の増配が XBRL で確かめられるまで
# 増配に数えない(「記念配当の実施（増配）」は増配分がまるごと記念配当のことが多い)。
SPECIAL = re.compile(r"記念配当|特別配当")
# 本文注記の「記念配当 6円00銭」「うち特別配当 10.00」など。直後が年・周年・期などなら金額ではない。
SPECIAL_AMT = re.compile(
    r"(記念|特別)配当[^0-9。]{0,12}?([0-9][0-9,]*(?:\.[0-9]+)?)(?![0-9.,])(?!\s*(?:年|周年|月|期|%|％|株))"
    r"(?:\s*円(?:\s*([0-9]{1,2})\s*銭)?)?")
FY_NOTE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月期")
# 記念配当を差し引くようにした版。上がったら XBRL を読み直す(xbrl_seen を捨てる)。
XBRL_VERSION = 2

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


def special_amounts(text: str) -> list[dict]:
    """本文から記念・特別配当の額を拾う。[{fy: (年, 月) or None, part, amount}]

    年度は直前に出てくる「2027年3月期」を採る。中間/期末は直前の語で見分ける。
    """
    out = []
    for m in SPECIAL_AMT.finditer(text):
        try:
            amt = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        if m.group(3):
            amt += int(m.group(3)) / 100
        if not (0 < amt < 10000):
            continue
        before = text[max(0, m.start() - 120):m.start()]
        fys = FY_NOTE.findall(before)
        fy = (int(fys[-1][0]), int(fys[-1][1])) if fys else None
        near = before[-40:]
        part = "期末" if "期末" in near else ("中間" if re.search(r"中間|第\s*2\s*四半期", near) else None)
        out.append({"fy": fy, "part": part, "amount": round(amt, 2)})
    # 同じ注記が表と本文の両方に出ることがあるので重複を除く
    uniq = {(d["fy"], d["part"], d["amount"]): d for d in out}
    return list(uniq.values())


def _sum_special(specials: list[dict], fy_year: int | None, *, unknown_counts: bool) -> float:
    tot = 0.0
    for d in specials:
        y = d["fy"][0] if d["fy"] else None
        if (y is None and unknown_counts) or (y is not None and y == fy_year):
            tot += d["amount"]
    return tot


def judge_xbrl(facts: dict[str, float], text: str = "", title: str = "") -> dict | None:
    """配当の増額(予想の上方修正、または前期比の増配予想)なら説明を返す。

    記念・特別配当は差し引いた普通配当で比べる。戻り値の kind は
    revision / tanshin(普通配当の増配)、special(増えたのは記念・特別配当だけ、
    または額が読めず普通配当の増配を確かめられない)。
    """
    text = unicodedata.normalize("NFKC", text or "")
    title_n = unicodedata.normalize("NFKC", title or "")
    mentions = bool(SPECIAL.search(text)) or bool(SPECIAL.search(title_n))
    specials = special_amounts(text)
    t_fy = FY_NOTE.search(title_n)
    t_year = int(t_fy.group(1)) if t_fy else None
    if t_year is None and specials:
        ys = [d["fy"][0] for d in specials if d["fy"]]
        t_year = max(ys) if ys else None

    def verdict(kind, before, after, base_label, last=None):
        sp_after = _sum_special(specials, fy_after, unknown_counts=True)
        sp_before = _sum_special(specials, fy_before, unknown_counts=False) if fy_before else 0.0
        o_after, o_before = after - sp_after, before - sp_before
        if mentions and not specials:
            # 記念・特別配当があるのに額が読めない。増配とは言えない。
            if after > before:
                return {"kind": "special", "detail": f"記念・特別配当を含む配当予想({base_label}{before:g}円→{after:g}円、普通配当の増減は不明)"}
            return None
        if o_after > o_before and o_before >= 0:
            if kind == "revision":
                tail = f"、前期{last:g}円" if last is not None else ""
                d = f"年間配当予想を{before:g}円→{after:g}円に増額修正{tail}"
            else:
                d = f"決算短信で{base_label}の年間配当予想{after:g}円(前期{before:g}円から増配)"
            if sp_after or sp_before:
                d += f"。記念・特別配当を除いた普通配当でも{o_before:g}円→{o_after:g}円"
            return {"kind": kind, "detail": d, "specials": specials}
        if after > before:
            return {"kind": "special", "detail": f"増えたのは記念・特別配当のみ({before:g}円→{after:g}円、普通配当は{o_before:g}円→{o_after:g}円)", "specials": specials}
        return None

    # 配当予想の修正(rvdf / rvfc): Previous=前回予想, Current=今回予想
    prev = _pick(facts, "CurrentYearDuration", "AnnualMember", "PreviousMember")
    cur = _pick(facts, "CurrentYearDuration", "AnnualMember", "CurrentMember", avoid=("Result",))
    if prev is not None and cur is not None:
        if prev < 0:
            return None
        last = _pick(facts, "PriorYearDuration", "AnnualMember", "Result")
        # 前回予想にも同じ記念配当が入っていたかは分からないので、今回予想からだけ引く(保守的)
        fy_after, fy_before = t_year, None
        return verdict("revision", prev, cur, "", last)
    # 決算短信: 本決算なら来期予想 vs 今期実績、四半期なら今期予想 vs 前期実績
    nxt = _pick(facts, "NextYearDuration", "AnnualMember", avoid=("Result",))
    if nxt is not None:
        base = _pick(facts, "CurrentYearDuration", "AnnualMember", "Result")
        label = "来期"
        fy_after = (t_year + 1) if (t_fy and t_year) else t_year
    else:
        nxt = _pick(facts, "CurrentYearDuration", "AnnualMember", "Forecast")
        base = _pick(facts, "PriorYearDuration", "AnnualMember", "Result")
        label = "今期"
        fy_after = t_year
    if nxt is None or base is None or base <= 0:
        return None
    fy_before = (fy_after - 1) if fy_after else None
    return verdict("tanshin", base, nxt, label)


def fetch_xbrl(name: str) -> tuple[dict[str, float], str] | None:
    try:
        r = requests.get(BASE_DIR + name, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        return None
    facts: dict[str, float] = {}
    texts: list[str] = []
    for n in zf.namelist():
        # 決算短信はサマリー(Summary/ 配下)、配当予想の修正は zip 直下の ixbrl
        if not n.endswith("-ixbrl.htm") or "/Attachment/" in n:
            continue
        html = zf.read(n).decode("utf-8", "ignore")
        facts.update(_dps_facts(html))
        texts.append(_text(html))   # 記念配当の内訳は注記の文章にしか無い
    return facts, " ".join(texts)


def _special_event(code: str, day: datetime, title: str, detail: str | None = None) -> dict:
    return {
        "code": code,
        "date": day.strftime("%Y-%m-%d"),
        "detail": detail or title,
        "earnings_up": bool(re.search(r"業績予想.*修正|上方修正", title)),
        "source": "TDnet",
        "kind": "special",
        "special": True,   # 記念・特別配当。増配には数えない
    }


def fetch_day(day: datetime, xbrl_seen: set[str] | None = None,
              specials_out: list[dict] | None = None) -> list[dict] | None:
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
            if SPECIAL.search(unicodedata.normalize("NFKC", title)):
                out.append(_special_event(code, day, title))
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
        with ThreadPoolExecutor(XBRL_WORKERS) as ex:
            results = list(ex.map(lambda j: fetch_xbrl(j[2]), xbrl_jobs))
        # 同じ銘柄に決算短信と配当予想の修正が並ぶことがある。普通配当の増配が
        # 1つでも確かめられればそれを採り、記念配当だけなら special にする。
        verdicts: dict[str, tuple[dict, str]] = {}
        for (code, title, name), got in zip(xbrl_jobs, results):
            if got is None:
                continue  # 取れなかった zip は次回また読む
            xbrl_seen.add(name)
            facts, text = got
            j = judge_xbrl(facts, text, title)
            if specials_out is not None:
                for sp in special_amounts(unicodedata.normalize("NFKC", text)):
                    if sp["fy"]:
                        specials_out.append({
                            "code": code, "fy_end": f"{sp['fy'][0]}-{sp['fy'][1]:02d}",
                            "part": sp["part"], "amount": sp["amount"],
                            "date": day.strftime("%Y-%m-%d"), "source": "TDnet XBRL",
                        })
            if not j:
                continue
            have = verdicts.get(code)
            if have is None or (have[0]["kind"] == "special" and j["kind"] != "special"):
                verdicts[code] = (j, title)
        n_hit = n_sp = 0
        by_code = {e["code"]: k for k, e in enumerate(out)}
        for code, (j, title) in verdicts.items():
            k = by_code.get(code)
            if j["kind"] == "special":
                n_sp += 1
                if k is None:
                    continue   # 記念配当だけの増額は、イベントとしては足さない
                # タイトルは「増配」でも、中身は記念・特別配当だけだった
                out[k] = _special_event(code, day, title, f"{j['detail']}（{title}）")
                continue
            if k is not None and not out[k].get("special"):
                continue   # タイトルで拾った増配はそのまま
            n_hit += 1
            ev = {
                "code": code,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"{j['detail']}（{title}）",
                "earnings_up": bool(re.search(r"上方修正", title)),
                "source": "TDnet XBRL",
                "kind": j["kind"],
            }
            if k is None:
                by_code[code] = len(out)
                out.append(ev)
            else:
                out[k] = ev   # 記念配当のタイトルでも、普通配当が増えていれば増配
        print(f"  {ymd}: XBRL {len(xbrl_jobs)} 件を確認、増配 {n_hit} 件、記念・特別配当のみ {n_sp} 件")
    return out


def mark_specials(events: list[dict]) -> int:
    """既存イベントのうち、タイトルが記念・特別配当のもの(普通配当の増配を
    XBRL で確かめていないもの)を special にする。戻り値は付け替えた件数。"""
    n = 0
    for e in events:
        if e.get("special") or e.get("special_checked"):
            continue
        if SPECIAL.search(unicodedata.normalize("NFKC", e.get("detail", ""))):
            e["special"] = True
            e["kind"] = "special"
            n += 1
    return n


def merge_specials(old: list[dict], new: list[dict]) -> list[dict]:
    """記念・特別配当の額の台帳。手入力(source=manual)も含め、捨てずに貯める。"""
    seen = {(d["code"], d["fy_end"], d.get("part"), d["amount"]) for d in old}
    out = list(old)
    for d in new:
        k = (d["code"], d["fy_end"], d.get("part"), d["amount"])
        if k not in seen:
            seen.add(k)
            out.append(d)
    return sorted(out, key=lambda d: (d["code"], d["fy_end"]))


def main() -> int:
    data = {"events": []}
    if OUT.exists():
        data = json.loads(OUT.read_text(encoding="utf-8"))
    events = data.get("events", [])
    n_marked = mark_specials(events)
    if n_marked:
        print(f"記念・特別配当のタイトルを増配から外した: {n_marked} 件")

    xbrl_seen = set(data.get("xbrl_seen", []))
    first_xbrl = "xbrl_seen" not in data
    # 判定方法を変えた版では、読み済みの XBRL も読み直す
    reread = data.get("xbrl_version") != XBRL_VERSION
    if reread:
        xbrl_seen = set()
    today = datetime.now(JST)
    lookback = LOOKBACK_DAYS if any(e.get("source") == "TDnet" for e in events) else BACKFILL_DAYS
    if first_xbrl or reread:
        lookback = BACKFILL_DAYS  # XBRL 判定を入れた/変えた最初の回は1か月分を読み直す
    new_specials: list[dict] = []
    added = 0
    ok_days = 0
    for i in range(lookback):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        got = fetch_day(day, xbrl_seen, new_specials)
        if got is None:
            continue
        ok_days += 1
        ymd = day.strftime("%Y-%m-%d")
        if reread:
            # 読み直した日は、その日の TDnet 由来のイベントを新しい判定で置き換える
            # (手動収集ぶんは残す)。古い XBRL 判定は記念配当込みで比べていた。
            events = [e for e in events if not (e["date"] == ymd and str(e.get("source", "")).startswith("TDnet"))]
        idx = {(e["code"], e["date"]): k for k, e in enumerate(events)}
        for e in got:
            k = idx.get((e["code"], e["date"]))
            if k is not None:
                # 既にある記念配当イベントを、普通配当の増配が確かめられたら差し替える
                if events[k].get("special") and not e.get("special") and not events[k].get("special_checked"):
                    events[k] = e
                continue
            idx[(e["code"], e["date"])] = len(events)
            events.append(e)
            added += 1

    cutoff = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    events = sorted((e for e in events if e["date"] >= cutoff), key=lambda e: (e["date"], e["code"]))
    data["events"] = events
    data["specials"] = merge_specials(data.get("specials", []), new_specials)
    data["updated_at"] = today.strftime("%Y-%m-%d")
    data["last_fetch"] = {"ok_days": ok_days, "added": added}
    if ok_days:
        data["xbrl_version"] = XBRL_VERSION
    # zip 名は「0812 + 日付時刻 + 連番.zip」。先頭4文字を除いた日付順で古いものから捨てる
    data["xbrl_seen"] = sorted(xbrl_seen, key=lambda n: n[4:])[-20000:]
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dividend hikes: +{added} (TDnet ok days {ok_days}, lookback {lookback}d), total {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
