"""Fetch TOPIX EPS history (monthly, multi-year).

TOPIX EPS = TOPIX month-end close ÷ TOPIX weighted-average PER.

Sources (all free, official / stable):
  1. Weighted PER — JPX monthly PER/PBR statistics.
     Index page:  https://www.jpx.co.jp/markets/statistics-equities/misc/04.html
     Monthly file: perpbr{YYYYMM}.xlsx  (linked under non-derivable hash dirs,
                   so we scrape the index page for the real URLs).
     Sheet 1 = 規模別・業種別（連結）. The market-total weighted PER is the FIRST
     row whose 種別(col 3) == '総合' — i.e. プライム市場 (2022/04-) or 市場一部
     (pre-2022/04), which is the TOPIX universe. Weighted PER = col 10 (加重＿PER).
     Gotcha: old files store numbers as formula literals ("=22.8") with no cached
     value, so we read data_only=False and strip a leading '='.
  2. TOPIX month-end close —
     a. Current month (authoritative): JPX monthly index report CSV.
     b. History backfill: WSJ Michelangelo timeseries API (INDEX/JP/XTKS/TPX),
        month-end values that match JPX exactly.

History accumulates: existing months are kept, only missing (+ the latest,
in case it was preliminary) perpbr months are downloaded each run.

Output: web/data/topix_eps_history.json
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import openpyxl

try:
    from curl_cffi import requests as _http
    _SESSION = _http.Session(impersonate="chrome")
except ImportError:  # pragma: no cover - fallback path
    import urllib.request
    _SESSION = None

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "topix_eps_history.json"

PER_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/04.html"
JPX_BASE = "https://www.jpx.co.jp"
JPX_MONTHLY_INDEX_CSV = (
    "https://www.jpx.co.jp/automation/markets/indices/related/report/files/"
    "monthlyindexreport_j.csv"
)
WSJ_TOKEN = "cecc4267a0194af89ca343805a3e57af"
WSJ_HISTORY_URL = "https://api.wsj.net/api/michelangelo/timeseries/history"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _get_bytes(url: str, headers: dict | None = None) -> bytes:
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    if _SESSION is not None:
        return _SESSION.get(url, timeout=25, headers=h).content
    import urllib.request
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=25).read()


def _get_text(url: str, headers: dict | None = None) -> str:
    return _get_bytes(url, headers).decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# 1. JPX weighted PER (perpbr monthly xlsx)
# ---------------------------------------------------------------------------

def list_perpbr_files() -> dict[str, str]:
    """Return {YYYYMM: absolute_url} scraped from the JPX index page."""
    html = _get_text(PER_INDEX_URL)
    out: dict[str, str] = {}
    for href, ym in re.findall(r'href="([^"]*perpbr(\d{6})\.xlsx)"', html):
        url = href if href.startswith("http") else JPX_BASE + href
        out[ym] = url
    return out


def _cell_num(v):
    """Coerce a perpbr cell to float. Handles formula literals like '=20.4'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.startswith("="):
        s = s[1:]
    s = s.replace(",", "")
    if s in ("", "-", "－", "＊", "*"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_perpbr(data: bytes) -> dict | None:
    """Extract the market-total weighted PER from a perpbr xlsx.

    Returns {section, per_weighted, net_income} or None.
    The market total is the FIRST data row whose 種別 (col index 3) == '総合'.
    """
    import io
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False, read_only=True)
    ws = wb.worksheets[0]
    for row in ws.iter_rows(values_only=True):
        if len(row) < 13:
            continue
        if str(row[3]).strip() == "総合":
            per = _cell_num(row[10])          # 加重＿PER（倍）
            if per is None:
                return None
            return {
                "section": str(row[1]).strip(),   # プライム市場 / 市場一部
                "per_weighted": round(per, 2),
                "net_income": _cell_num(row[12]),  # 加重 aggregate net income (¥)
            }
    return None


# ---------------------------------------------------------------------------
# 2. TOPIX month-end closes
# ---------------------------------------------------------------------------

def fetch_topix_closes_wsj() -> dict[str, float]:
    """WSJ monthly TOPIX month-end closes → {YYYY-MM: close}. Matches JPX."""
    body = {
        "Step": "P1M",
        "TimeFrame": "P10Y",
        "EntitlementToken": WSJ_TOKEN,
        "IncludeMockTick": True,
        "Series": [{
            "Key": "INDEX/JP/XTKS/TPX", "Dialect": "Charting", "Kind": "Ticker",
            "SeriesId": "s1", "DataTypes": ["Last"], "Indicators": [],
        }],
    }
    url = WSJ_HISTORY_URL + "?json=" + urllib.parse.quote(json.dumps(body)) + "&ckey=cecc4267a0"
    headers = {
        "Dylan2010.EntitlementToken": WSJ_TOKEN,
        "Origin": "https://www.wsj.com",
        "Referer": "https://www.wsj.com/",
    }
    try:
        d = json.loads(_get_text(url, headers))
    except Exception as e:  # pragma: no cover
        print(f"  WARN WSJ fetch failed: {e}", file=sys.stderr)
        return {}
    ticks = d.get("TimeInfo", {}).get("Ticks", [])
    points = d.get("Series", [{}])[0].get("DataPoints", [])
    out: dict[str, float] = {}
    for t, val in zip(ticks, points):
        dt = datetime.fromtimestamp(t / 1000, tz=timezone.utc).date()
        v = val[0] if isinstance(val, list) else val
        if v is None:
            continue
        out[f"{dt.year:04d}-{dt.month:02d}"] = round(float(v), 2)
    return out


def fetch_topix_close_current() -> tuple[str, float] | None:
    """JPX official current-month TOPIX close → (YYYY-MM, close). Authoritative."""
    try:
        raw = _get_bytes(JPX_MONTHLY_INDEX_CSV)
    except Exception as e:  # pragma: no cover
        print(f"  WARN JPX monthly CSV failed: {e}", file=sys.stderr)
        return None
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        text = raw.decode("shift_jis", "ignore")
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 3 and parts[1] == "TOPIX":
            m = re.match(r"(\d{4})(\d{2})(\d{2})", parts[0])
            if not m:
                continue
            try:
                close = float(parts[2].replace(",", ""))
            except ValueError:
                continue
            return f"{m.group(1)}-{m.group(2)}", close
    return None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _month_end_date(ym: str) -> str:
    """'YYYY-MM' -> ISO month-end date string."""
    y, m = int(ym[:4]), int(ym[5:7])
    first_next = datetime(y + (m // 12), (m % 12) + 1, 1)
    return (first_next - timedelta(days=1)).date().isoformat()


def compute_yoy(history: list[dict]) -> None:
    eps_by_ym = {h["month"]: h.get("eps") for h in history if h.get("eps")}
    for h in history:
        cur = h.get("eps")
        if not cur:
            h["eps_yoy_pct"] = None
            continue
        y, m = int(h["month"][:4]), int(h["month"][5:7])
        prior_ym = f"{y - 1:04d}-{m:02d}"
        prior = eps_by_ym.get(prior_ym)
        h["eps_yoy_pct"] = round((cur / prior - 1) * 100, 2) if prior else None


def main() -> int:
    # Load existing history
    existing: dict[str, dict] = {}
    preserved: dict = {}
    if OUTPUT_PATH.exists():
        try:
            full = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            for h in full.get("history", []):
                existing[h["month"]] = h
            for k in ("commentary", "level_judgment", "commentary_model"):
                if k in full:
                    preserved[k] = full[k]
        except Exception:
            existing = {}

    print("Listing JPX perpbr files...", file=sys.stderr)
    perpbr = list_perpbr_files()
    print(f"  {len(perpbr)} monthly PER files on index page", file=sys.stderr)
    if not perpbr:
        print("ERROR: no perpbr files found; aborting", file=sys.stderr)
        return 1

    # Which months to (re)download: those missing a PER + always the newest one.
    all_months = sorted(perpbr)
    newest = all_months[-1] if all_months else None
    to_fetch = [
        ym for ym in all_months
        if f"{ym[:4]}-{ym[4:]}" not in existing
        or existing.get(f"{ym[:4]}-{ym[4:]}", {}).get("per_weighted") is None
        or ym == newest
    ]
    print(f"  fetching PER for {len(to_fetch)} month(s)", file=sys.stderr)

    per_by_ym: dict[str, dict] = {}
    for ym in to_fetch:
        key = f"{ym[:4]}-{ym[4:]}"
        try:
            info = parse_perpbr(_get_bytes(perpbr[ym]))
            if info:
                per_by_ym[key] = info
        except Exception as e:
            print(f"  WARN perpbr {ym}: {e}", file=sys.stderr)

    print("Fetching TOPIX closes (WSJ history)...", file=sys.stderr)
    closes = fetch_topix_closes_wsj()
    print(f"  {len(closes)} monthly closes", file=sys.stderr)
    cur = fetch_topix_close_current()
    if cur:
        closes[cur[0]] = cur[1]  # authoritative override for the current month
        print(f"  JPX current: {cur[0]} = {cur[1]}", file=sys.stderr)

    # Merge into history keyed by month
    merged: dict[str, dict] = dict(existing)
    months = set(existing) | set(per_by_ym) | {f"{ym[:4]}-{ym[4:]}" for ym in perpbr}
    for month in months:
        row = dict(merged.get(month, {"month": month}))
        row["month"] = month
        row["date"] = _month_end_date(month)
        if month in per_by_ym:
            row["per_weighted"] = per_by_ym[month]["per_weighted"]
            row["section"] = per_by_ym[month]["section"]
            row["net_income"] = per_by_ym[month]["net_income"]
        if month in closes:
            row["topix_close"] = closes[month]
        per = row.get("per_weighted")
        close = row.get("topix_close")
        row["eps"] = round(close / per, 2) if (per and close) else row.get("eps")
        merged[month] = row

    history = [merged[m] for m in sorted(merged)]
    # Keep only rows that have at least a PER (a close-only row has no EPS meaning)
    history = [h for h in history if h.get("per_weighted") is not None]
    compute_yoy(history)

    latest = history[-1] if history else {}
    now_jst = datetime.now(timezone(timedelta(hours=9)))
    output = {
        "asof": latest.get("month", ""),
        "generated_at_jst": now_jst.isoformat(),
        "source": "JPX 規模別・業種別PER (加重平均・連結) × TOPIX月末終値 (JPX/WSJ)",
        "metric_notes": {
            "per_weighted": "加重平均PER(連結)。プライム市場(2022/04-)/市場一部(-2022/03)の総合。TOPIX母集団の代理",
            "topix_close": "TOPIX指数の月末終値。当月はJPX公式、過去はWSJ(JPX一致)",
            "eps": "TOPIX月末終値 ÷ 加重平均PER (円)",
            "eps_yoy_pct": "12ヶ月前との比較(%)",
        },
        "history": history,
    }
    output.update(preserved)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB, {len(history)} months)", file=sys.stderr)
    if latest:
        print(
            f"  Latest {latest['month']}: TOPIX {latest.get('topix_close','-')} "
            f"PER(加重) {latest.get('per_weighted','-')} "
            f"EPS ¥{latest.get('eps','-')} "
            f"YoY {latest.get('eps_yoy_pct','-')}%",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
