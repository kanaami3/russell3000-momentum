"""Fetch JPX weekly Trading by Type of Investors (投資部門別売買状況).

Source: https://www.jpx.co.jp/markets/statistics-equities/investor-type/
  Each weekly file `stock_val_1_YYMM0W.xls` (Trading Value) contains TWO weeks
  of data (prior + current) on the "TSE Prime" sheet, with a 差引き (net) column
  per week for every investor category (海外投資家 / 個人 / 信託銀行 / ...).

We parse the net (buy - sell) balance per category per week, accumulate into a
weekly history keyed by the week-ending (Friday) date, and emit a judgment on
the latest week's foreign vs. individual flows.

Units: source is 千円 (thousand yen). We store net in 億円 (`net_oku`) for display
and keep raw 千円 (`net_thousand`) for precision.

Output: web/data/investor_flows_jp.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import xlrd

try:
    from curl_cffi import requests as _http
    _SESSION = _http.Session(impersonate="chrome")
except ImportError:  # pragma: no cover
    import urllib.request
    _SESSION = None

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "investor_flows_jp.json"

INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"
BASE = "https://www.jpx.co.jp"

# Investor categories to track (JPX label in col 0 -> our key / display name).
CATEGORIES = {
    "海外投資家": ("foreign", "海外投資家"),
    "個": ("individual", "個人"),          # label is "個　人" (ideographic space)
    "信託銀行": ("trust_bank", "信託銀行"),
    "投資信託": ("investment_trust", "投資信託"),
    "事業法人": ("business_corp", "事業法人"),
    "証券会社": ("securities_cos", "証券会社"),
    "生保・損保": ("insurance", "生保・損保"),
    "都銀・地銀等": ("banks", "都銀・地銀等"),
}


def _http_get(url: str) -> bytes:
    if _SESSION is not None:
        return _SESSION.get(url, timeout=25).content
    return urllib.request.urlopen(url, timeout=25).read()


def _http_get_text(url: str) -> str:
    if _SESSION is not None:
        return _SESSION.get(url, timeout=25).text
    return urllib.request.urlopen(url, timeout=25).read().decode("utf-8", "ignore")


def list_weekly_value_files() -> list[str]:
    """Return absolute URLs of every `stock_val_1_*.xls` on the index page."""
    html = _http_get_text(INDEX_URL)
    out = []
    for m in re.finditer(r'href="([^"]*stock_val_1_\d{6}\.xls)"', html):
        url = m.group(1) if m.group(1).startswith("http") else BASE + m.group(1)
        out.append(url)
    return sorted(set(out), reverse=True)


def list_weekly_xlsx_files() -> list[tuple[str, str, str]]:
    """New format from 2026-09 week3: stock_1_w_YYYYMMDD_YYYYMMDD.xlsx (1 week/file).

    Returns [(start_iso, end_iso, url)], newest first.
    """
    html = _http_get_text(INDEX_URL)
    out = set()
    for m in re.finditer(r'href="([^"]*stock_1_w_(\d{8})_(\d{8})\.xlsx)"', html):
        url = m.group(1) if m.group(1).startswith("http") else BASE + m.group(1)
        a, b = m.group(2), m.group(3)
        out.add((f"{a[:4]}-{a[4:6]}-{a[6:]}", f"{b[:4]}-{b[4:6]}-{b[6:]}", url))
    return sorted(out, key=lambda x: x[1], reverse=True)


# 新形式の列グループ(売/買/差引/合計の4列)を、見出し3段のどれに出る名前で分類するか。
# 海外投資家は「法人」「個人」、個人は「現金」「信用」に分かれているので合算する。
_XLSX_TOP = {"海外投資家": "foreign", "個人": "individual", "証券会社": "securities_cos"}
_XLSX_LEAF = {"投資信託": "investment_trust", "事業法人": "business_corp",
              "生保・損保": "insurance", "都銀・地銀等": "banks", "信託銀行": "trust_bank"}


def parse_value_xlsx(raw: bytes, start: str, end: str) -> dict | None:
    """Parse one new-format weekly file -> one week record (東証プライム, 金額)."""
    import io
    import openpyxl
    try:
        ws = openpyxl.load_workbook(io.BytesIO(raw), data_only=True).worksheets[0]
    except Exception:
        return None
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    def s(v):
        return "" if v is None else str(v).replace("　", "").replace(" ", "")

    hdr = next((i for i, r in enumerate(rows) if sum("差引" in s(v) for v in r) >= 3), None)
    if hdr is None or hdr < 3:
        return None
    prime = next((i for i, r in enumerate(rows) if len(r) > 1 and "東証プライム" in s(r[1])), None)
    if prime is None:
        return None
    val_row = next((rows[i] for i in range(prime, min(prime + 3, len(rows)))
                    if len(rows[i]) > 2 and "金額" in s(rows[i][2])), None)
    if val_row is None:
        return None

    def label_at(r, c):
        for j in range(c, -1, -1):
            if s(rows[r][j]):
                return s(rows[r][j])
        return ""

    nets: dict[str, float] = {}
    for c, v in enumerate(rows[hdr]):
        if "差引" not in s(v) or c >= len(val_row) or not isinstance(val_row[c], (int, float)):
            continue
        g = c - 2  # group starts at 売
        top = label_at(hdr - 3, g)
        leaf = label_at(hdr - 1, g)
        key = next((k for n, k in _XLSX_TOP.items() if top.startswith(n)), None)
        if key is None:
            key = next((k for n, k in _XLSX_LEAF.items() if leaf.startswith(n)), None)
        if key:
            nets[key] = nets.get(key, 0.0) + float(val_row[c])
    if "foreign" not in nets:
        return None
    names = {v[0]: v[1] for v in CATEGORIES.values()}
    sm, sd = int(start[5:7]), int(start[8:])
    em, ed = int(end[5:7]), int(end[8:])
    return {
        "date": end,
        "week_label": f"{end[:4]}年{em}月 ( {sm}/{sd} - {em}/{ed} )",
        "flows": {k: {"name": names[k], "net_thousand": round(v, 0), "net_oku": round(v / 100_000, 1)}
                  for k, v in nets.items()},
    }


def _parse_range_end(range_str: str, year: int) -> str | None:
    """'06/29～07/03' -> '2026-07-03' (the week-ending Friday)."""
    m = re.search(r"(\d{1,2})/(\d{1,2})\s*[〜～\-]\s*(\d{1,2})/(\d{1,2})", range_str)
    if not m:
        return None
    end_mm, end_dd = int(m.group(3)), int(m.group(4))
    # Handle a range that straddles a year boundary (Dec -> Jan)
    start_mm = int(m.group(1))
    yr = year
    if start_mm == 12 and end_mm == 1:
        yr = year + 1
    return f"{yr:04d}-{end_mm:02d}-{end_dd:02d}"


def parse_value_file(raw: bytes) -> list[dict]:
    """Parse one weekly file -> up to 2 week records (prior + current).

    Each record: {date, week_label, flows: {key: {name, net_thousand, net_oku}}}
    """
    try:
        wb = xlrd.open_workbook(file_contents=raw)
        sh = wb.sheet_by_name("TSE Prime")
    except Exception:
        try:
            sh = wb.sheet_by_index(0)
        except Exception:
            return []

    def cell(i, j):
        try:
            return sh.cell_value(i, j)
        except Exception:
            return ""

    def cell_s(i, j):
        return str(cell(i, j)).strip()

    # Year from the title week-label row (e.g. "2026年7月第1週 ... ( 6/29 - 7/3 )")
    year = datetime.now().year
    week_label = ""
    for i in range(min(8, sh.nrows)):
        t = cell_s(i, 0)
        ym = re.search(r"(\d{4})年", t)
        if ym:
            year = int(ym.group(1))
            week_label = t
            break

    # Locate the header row that carries '差引き' to find the two balance columns
    # and the row that carries the date ranges.
    bal_cols: list[int] = []
    range_row = None
    header_row = None
    for i in range(min(20, sh.nrows)):
        rowvals = [cell_s(i, j) for j in range(sh.ncols)]
        if any("差引" in v for v in rowvals):
            bal_cols = [j for j, v in enumerate(rowvals) if "差引" in v]
            header_row = i
        if any(re.search(r"\d{1,2}/\d{1,2}\s*[〜～\-]\s*\d{1,2}/\d{1,2}", v) for v in rowvals):
            range_row = i
    if not bal_cols:
        return []

    # Map each balance column -> its week-ending date (from the nearest range cell).
    col_dates: dict[int, str] = {}
    if range_row is not None:
        ranges = {j: cell_s(range_row, j) for j in range(sh.ncols)
                  if re.search(r"\d{1,2}/\d{1,2}\s*[〜～\-]\s*\d{1,2}/\d{1,2}", cell_s(range_row, j))}
        range_cols = sorted(ranges)
        for bc in bal_cols:
            # the range column governing this balance col is the closest one at/left of it
            gov = max([rc for rc in range_cols if rc <= bc], default=None)
            if gov is not None:
                d = _parse_range_end(ranges[gov], year)
                if d:
                    col_dates[bc] = d

    # Collect category net balances by scanning col-0 labels.
    # For each category the label appears on the 売り (Sales) row; the 買い row is +1.
    records: dict[str, dict] = {}  # date -> {flows...}
    for i in range(sh.nrows):
        label = cell_s(i, 0).replace("　", "").replace(" ", "")
        matched = None
        for pat, (key, name) in CATEGORIES.items():
            p = pat.replace("　", "").replace(" ", "")
            if label.startswith(p):
                matched = (key, name)
                break
        if not matched:
            continue
        key, name = matched
        # sell row = i, buy row = i+1; net (差引き) sits on whichever row is non-empty
        for bc in bal_cols:
            date = col_dates.get(bc)
            if not date:
                continue
            net = None
            for r in (i, i + 1):
                v = cell(r, bc)
                if isinstance(v, (int, float)) and v != 0:
                    net = float(v)
                    break
                if isinstance(v, str):
                    vs = v.replace(",", "").strip()
                    if re.fullmatch(r"-?\d+(\.\d+)?", vs):
                        net = float(vs)
                        break
            if net is None:
                continue
            rec = records.setdefault(date, {"date": date, "week_label": week_label, "flows": {}})
            rec["flows"][key] = {
                "name": name,
                "net_thousand": round(net, 0),
                "net_oku": round(net / 100_000, 1),  # 千円 -> 億円
            }
    return list(records.values())


def classify(latest: dict) -> dict:
    f = latest["flows"].get("foreign", {}).get("net_oku")
    ind = latest["flows"].get("individual", {}).get("net_oku")

    def dir_word(v):
        if v is None:
            return "不明"
        return "買い越し" if v > 0 else ("売り越し" if v < 0 else "中立")

    fw, iw = dir_word(f), dir_word(ind)
    # Simple regime read: foreigners drive the tape; individuals often contrarian.
    if f is not None and f > 0:
        band, color = "海外買い越し", "emerald"
    elif f is not None and f < 0:
        band, color = "海外売り越し", "rose"
    else:
        band, color = "中立", "slate"
    parts = []
    if f is not None:
        parts.append(f"海外 {f:+,.0f}億円({fw})")
    if ind is not None:
        parts.append(f"個人 {ind:+,.0f}億円({iw})")
    return {"band": band, "label": " / ".join(parts), "color": color}


def main() -> int:
    existing: list[dict] = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("history", [])
        except Exception:
            existing = []

    by_date = {h["date"]: h for h in existing}
    print("Fetching JPX investor-type weekly files...", file=sys.stderr)
    try:
        for url in list_weekly_value_files():
            raw = _http_get(url)
            for rec in parse_value_file(raw):
                if not rec.get("flows"):
                    continue
                by_date[rec["date"]] = rec  # newest parse wins (dedupe by week)
                fo = rec["flows"].get("foreign", {}).get("net_oku")
                ind = rec["flows"].get("individual", {}).get("net_oku")
                print(f"  + {rec['date']}: 海外 {fo} / 個人 {ind} 億円", file=sys.stderr)
    except Exception as e:
        print(f"  WARN fetch/parse failed: {e}", file=sys.stderr)
    try:
        for start, end, url in list_weekly_xlsx_files():
            rec = parse_value_xlsx(_http_get(url), start, end)
            if not rec:
                print(f"  WARN could not parse {url}", file=sys.stderr)
                continue
            by_date[rec["date"]] = rec
            fo = rec["flows"].get("foreign", {}).get("net_oku")
            ind = rec["flows"].get("individual", {}).get("net_oku")
            print(f"  + {rec['date']} (xlsx): 海外 {fo} / 個人 {ind} 億円", file=sys.stderr)
    except Exception as e:
        print(f"  WARN xlsx fetch/parse failed: {e}", file=sys.stderr)

    history = sorted(by_date.values(), key=lambda x: x["date"])
    if not history:
        print("ERROR: no investor-flow data assembled", file=sys.stderr)
        return 1

    latest = history[-1]
    output = {
        "asof": latest["date"],
        "generated_at_jst": datetime.now().astimezone().isoformat(),
        "source": "JPX 投資部門別売買状況 (東証プライム, Trading Value) "
                  "https://www.jpx.co.jp/markets/statistics-equities/investor-type/",
        "unit_note": "net_oku = 差引き(買い-売り) 億円。プラス=買い越し / マイナス=売り越し。東証プライム・委託内訳。",
        "categories": [v[1] for v in CATEGORIES.values()],
        "latest_judgment": classify(latest),
        "history": history,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(
        f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB, {len(history)} weeks). "
        f"Latest {latest['date']}: {output['latest_judgment']['label']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
