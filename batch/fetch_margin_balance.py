"""Fetch JPX weekly margin balance (信用取引現在高) and derive 信用倍率.

Sources:
  1. Latest weeks: scrape the index page for the most recent weekly snapshots
     (mtseisan{YYYYMMDD}00.xls) — shows 二市場計 売残高 / 買残高 per week
  2. Historical: the past archive file (tvdivq000...xls) contains weekly data
     going back to 2013 — fetched on first run to seed the history

信用倍率 (margin ratio) interpretation:
  - <1     売り方優勢(踏み上げ余地)
  - 1〜3   健全
  - 3〜5   買い偏重・警戒開始
  - 5〜7   過熱・上値が重くなる
  - 7+     極度の買い偏重 → 急落リスク要警戒(過去事例: 2007 / 2018 / 2024)

Output: web/data/margin_balance_history.json
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import xlrd

try:
    from curl_cffi import requests as _http
    _SESSION = _http.Session(impersonate="chrome")
except ImportError:
    import urllib.request
    _SESSION = None

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "margin_balance_history.json"

INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/04.html"
HISTORY_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/06.html"
BASE = "https://www.jpx.co.jp"


def _http_get(url: str) -> bytes:
    if _SESSION is not None:
        return _SESSION.get(url, timeout=20).content
    return urllib.request.urlopen(url, timeout=20).read()


def _http_get_text(url: str) -> str:
    if _SESSION is not None:
        return _SESSION.get(url, timeout=20).text
    return urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# Latest weekly snapshots from the index page
# ---------------------------------------------------------------------------

def list_latest_weekly_files() -> list[tuple[str, str]]:
    """Return [(YYYY-MM-DD, full_url), ...] for the latest snapshots listed."""
    html = _http_get_text(INDEX_URL)
    out = []
    for m in re.finditer(r'href="([^"]*mtseisan(\d{8})00\.xls)"', html):
        url = m.group(1) if m.group(1).startswith("http") else BASE + m.group(1)
        d = m.group(2)
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        out.append((iso, url))
    # newest first
    return sorted(set(out), reverse=True)


def parse_latest_snapshot(raw: bytes) -> dict | None:
    """Parse one weekly mtseisan xls — return {date, sales_*, purchases_*}."""
    try:
        wb = xlrd.open_workbook(file_contents=raw)
        sh = wb.sheet_by_name("レイアウト")
    except Exception:
        return None

    # The file's title row has "信用取引現在高(YYYY/M/D申...)" — extract the asof
    title = str(sh.cell_value(0, 0))
    m = re.search(r"(\d{4})/(\d+)/(\d+)", title)
    asof = (
        f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if m else None
    )

    # Look for "二市場計 + 株数Shs." row (typically row index 6) and
    # the following row "金額Val." (typically index 7).
    sales_shares = purchases_shares = sales_value = purchases_value = None
    for i in range(min(20, sh.nrows)):
        col_b = str(sh.cell_value(i, 1)) if sh.ncols > 1 else ""
        col_c = str(sh.cell_value(i, 2)) if sh.ncols > 2 else ""
        if "二市場計" in col_b and "株数" in col_c:
            try:
                sales_shares = float(sh.cell_value(i, 3))
                purchases_shares = float(sh.cell_value(i, 5))
            except (ValueError, TypeError, IndexError):
                pass
            # next row is 金額
            if i + 1 < sh.nrows and "金額" in str(sh.cell_value(i + 1, 2)):
                try:
                    sales_value = float(sh.cell_value(i + 1, 3))
                    purchases_value = float(sh.cell_value(i + 1, 5))
                except (ValueError, TypeError, IndexError):
                    pass
            break

    if not asof or sales_value is None or purchases_value is None:
        return None

    return {
        "date": asof,
        "sales_shares_thou":    round(sales_shares, 0) if sales_shares is not None else None,
        "purchases_shares_thou": round(purchases_shares, 0) if purchases_shares is not None else None,
        "sales_value_mil":      round(sales_value, 0),
        "purchases_value_mil":  round(purchases_value, 0),
        "ratio_by_shares": round(purchases_shares / sales_shares, 2)
            if sales_shares and sales_shares > 0 else None,
        "ratio_by_value":  round(purchases_value / sales_value, 2)
            if sales_value and sales_value > 0 else None,
    }


# ---------------------------------------------------------------------------
# Historical (one-time seed) from 06.html archive
# ---------------------------------------------------------------------------

def fetch_history_archive_url() -> str | None:
    """Find the most recent historical xls on the 過去推移表 page."""
    html = _http_get_text(HISTORY_PAGE)
    candidates = re.findall(r'href="([^"]+\.xls)"', html)
    if not candidates:
        return None
    # Prefer the newest-looking file
    return candidates[-1] if candidates[-1].startswith("http") else BASE + candidates[-1]


def parse_historical_archive(raw: bytes) -> list[dict]:
    """Parse the long-history archive xls. Returns list of {date, ratios}.

    Header layout (sheet "信用取引現在高"):
      r4-9: nested headers (月日 | 委託 売残 株数/金額 / 買残 株数/金額 | 自己 ...)
      r10+: data rows
    Columns 1=売残株数, 2=売残金額, 3=買残株数, 4=買残金額 (under 委託)
    """
    try:
        wb = xlrd.open_workbook(file_contents=raw)
        sh = wb.sheet_by_name("信用取引現在高")
    except Exception:
        return []

    out = []
    for i in range(9, sh.nrows):
        date_cell = sh.cell_value(i, 0)
        # Date may be float (Excel serial), string "2024/01/05", or empty
        date_iso = _coerce_excel_date(date_cell, wb)
        if not date_iso:
            continue
        try:
            ss = float(sh.cell_value(i, 1))
            sv = float(sh.cell_value(i, 2))
            ps = float(sh.cell_value(i, 3))
            pv = float(sh.cell_value(i, 4))
        except (ValueError, TypeError):
            continue
        if sv <= 0 or ss <= 0:
            continue
        out.append({
            "date": date_iso,
            "sales_shares_thou":    round(ss, 0),
            "purchases_shares_thou": round(ps, 0),
            "sales_value_mil":      round(sv, 0),
            "purchases_value_mil":  round(pv, 0),
            "ratio_by_shares": round(ps / ss, 2),
            "ratio_by_value":  round(pv / sv, 2),
        })
    return out


def _coerce_excel_date(v, wb) -> str | None:
    if isinstance(v, str):
        m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", v)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return None
    if isinstance(v, (int, float)) and v > 30000:  # Excel serial date
        try:
            tup = xlrd.xldate_as_tuple(v, wb.datemode)
            return f"{tup[0]:04d}-{tup[1]:02d}-{tup[2]:02d}"
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_ratio(ratio: float | None) -> dict:
    if ratio is None:
        return {"band": "判定不能", "label": "-", "color": "slate"}
    if ratio < 1:
        return {"band": "売り方優勢", "label": f"売り方優勢({ratio:.2f}倍) — 踏み上げ余地・逆張り環境",
                "color": "emerald"}
    if ratio < 3:
        return {"band": "健全", "label": f"健全({ratio:.2f}倍)", "color": "blue"}
    if ratio < 5:
        return {"band": "買い偏重", "label": f"買い偏重({ratio:.2f}倍) — 警戒開始", "color": "amber"}
    if ratio < 7:
        return {"band": "過熱", "label": f"過熱({ratio:.2f}倍) — 上値が重い",
                "color": "orange"}
    return {"band": "極度の過熱", "label": f"極度の過熱({ratio:.2f}倍) — 急落リスク要警戒",
            "color": "rose"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    existing: list[dict] = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("history", [])
        except Exception:
            existing = []

    # 1. Seed history from archive on first run (or if history is short)
    if len(existing) < 100:
        print("Fetching historical archive (one-time seed)...", file=sys.stderr)
        try:
            arch_url = fetch_history_archive_url()
            if arch_url:
                arch_raw = _http_get(arch_url)
                hist = parse_historical_archive(arch_raw)
                print(f"  parsed {len(hist)} weekly rows from archive", file=sys.stderr)
                existing = hist
        except Exception as e:
            print(f"  WARN historical seed failed: {e}", file=sys.stderr)

    # 2. Append the latest weekly snapshots from the index page
    print("Fetching latest weekly snapshots...", file=sys.stderr)
    by_date = {h["date"]: h for h in existing}
    try:
        for iso, url in list_latest_weekly_files():
            if iso in by_date:
                continue
            raw = _http_get(url)
            row = parse_latest_snapshot(raw)
            if row:
                by_date[row["date"]] = row
                print(f"  + {row['date']}: ratio_by_value {row['ratio_by_value']}", file=sys.stderr)
    except Exception as e:
        print(f"  WARN latest snapshots failed: {e}", file=sys.stderr)

    history = sorted(by_date.values(), key=lambda x: x["date"])
    if not history:
        print("ERROR: no margin data assembled", file=sys.stderr)
        return 1

    latest = history[-1]
    output = {
        "asof": latest["date"],
        "generated_at_jst": datetime.now().astimezone().isoformat(),
        "source": "JPX Margin Statistics (https://www.jpx.co.jp/markets/statistics-equities/margin/)",
        "metric_notes": {
            "ratio_by_value":  "買残金額 / 売残金額(主流の信用倍率)",
            "ratio_by_shares": "買残株数 / 売残株数(株数ベース、低位株含む)",
            "bands": "1未満=売り方優勢 / 1-3=健全 / 3-5=買い偏重・警戒 / 5-7=過熱 / 7+=極度の過熱",
        },
        "latest_judgment": classify_ratio(latest.get("ratio_by_value")),
        "history": history,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(
        f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB, {len(history)} weeks). "
        f"Latest {latest['date']}: 信用倍率(金額ベース) {latest.get('ratio_by_value','-')}倍 "
        f"→ {output['latest_judgment']['label']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
