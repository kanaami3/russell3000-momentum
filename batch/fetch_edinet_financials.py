"""有価証券報告書(EDINET)から、10期分の主要な経営指標を集める。

**なぜ EDINET か。**
yfinance の日本株は年次財務が4〜5期しか返らず、J-Quants は無料プランで2期分、
10期分は有料の上位プランになる。10年の実績を見るには足りない。

有価証券報告書の冒頭「主要な経営指標等の推移」には、EPS・BPS・1株配当・
自己資本比率・ROE・配当性向・純利益・総資産が **5期分** 載っている。
最新の有報と、その5年前の有報の2通を読めば、ちょうど直近10期がそろう。
EDINET は金融庁の公式システムで、API は無料(要APIキー)。

**10期より古いデータは使わない。**
ここで集めるのは直近10期をカバーするのに必要な有報だけで、
判定(track_record.py)も直近10期の窓しか見ない。

処理の流れ(何度でも途中から再開できる):
  1. 書類一覧APIを日付ごとに叩き、有価証券報告書(docTypeCode=120)の
     docID・決算期末・提出日を data/edinet_asr_index.csv に貯める。
     どこまで走査したかは data/edinet_state.json に残す。
  2. 銘柄ごとに、直近10期をカバーする有報(普通は2通)を選び、
     まだ読んでいないものだけを CSV 形式(type=5)でダウンロードして
     「主要な経営指標等の推移」を抜き出し data/financial_history_jp.csv に足す。
  3. 時間の上限(EDINET_TIME_BUDGET_MIN)に達したらそこで保存して終わる。
     初回は数千通を読むので数回に分かれるが、次の実行が続きから進める。

入力:  環境変数 EDINET_API_KEY(無ければ何もせず終了コード0で抜ける)
出力:  data/edinet_asr_index.csv, data/edinet_state.json, data/financial_history_jp.csv
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
INDEX_CSV = Path(os.getenv("EDINET_INDEX_CSV", DATA / "edinet_asr_index.csv"))
STATE_JSON = Path(os.getenv("EDINET_STATE_JSON", DATA / "edinet_state.json"))
OUTPUT_CSV = Path(os.getenv("FIN_HISTORY_CSV", DATA / "financial_history_jp.csv"))
UNIVERSE_FILES = [DATA / "universe_jp.json", DATA / "universe_jp_ext.json"]

API = "https://api.edinet-fsa.go.jp/api/v2"
WINDOW_FY = 10
# 直近の有報(過去15か月以内に提出)と、その5年前の有報(5年3か月前ごろ提出)を
# 拾えれば10期そろう。余裕を見て6年半さかのぼって走査する。
SCAN_YEARS = float(os.getenv("EDINET_SCAN_YEARS", "6.5"))
TIME_BUDGET_SEC = float(os.getenv("EDINET_TIME_BUDGET_MIN", "150")) * 60
SLEEP_SEC = float(os.getenv("EDINET_SLEEP_SEC", "0.3"))
SAVE_EVERY = 100

INDEX_FIELDS = ["code", "doc_id", "period_end", "submitted"]
OUT_FIELDS = ["code", "doc_id", "submitted", "fy_end", "consolidated",
              "eps", "bps", "dps", "payout", "equity_ratio", "roe",
              "net_income", "total_assets"]

# --- 「主要な経営指標等の推移」の要素 ------------------------------------------
# 要素名は会計基準(日本基準・IFRS・米国基準)で少しずつ違うので、前方一致の
# 正規表現で拾う。先に書いたものほど優先する。
METRIC_PATTERNS: dict[str, list[str]] = {
    "eps": [r"BasicEarningsLossPerShare\w*SummaryOfBusinessResults"],
    "bps": [r"NetAssetsPerShare\w*SummaryOfBusinessResults",
            r"EquityAttributableToOwnersOfParentPerShare\w*SummaryOfBusinessResults",
            r"ShareholdersEquityPerShare\w*SummaryOfBusinessResults",
            r"EquityPerShare\w*SummaryOfBusinessResults"],
    "dps": [r"DividendPaidPerShare\w*SummaryOfBusinessResults"],
    "payout": [r"PayoutRatio\w*SummaryOfBusinessResults"],
    "equity_ratio": [r"EquityToAssetRatio\w*SummaryOfBusinessResults",
                     r"RatioOfOwnersEquityToGrossAssets\w*SummaryOfBusinessResults",
                     r"EquityAttributableToOwnersOfParentToTotalAssets\w*SummaryOfBusinessResults",
                     r"ShareholdersEquityToTotalAssets\w*SummaryOfBusinessResults"],
    "roe": [r"RateOfReturnOnEquity\w*SummaryOfBusinessResults",
            r"RatioOfProfitToEquityAttributableToOwnersOfParent\w*SummaryOfBusinessResults"],
    "net_income": [r"ProfitLossAttributableToOwnersOfParent\w*SummaryOfBusinessResults",
                   r"NetIncomeLossAttributableToOwnersOfParent\w*SummaryOfBusinessResults",
                   r"NetIncomeLoss\w*SummaryOfBusinessResults",
                   r"ProfitLoss\w*SummaryOfBusinessResults"],
    "total_assets": [r"TotalAssets\w*SummaryOfBusinessResults"],
}
_COMPILED = {k: [re.compile(r"^(?:[a-z0-9\-]+_cor:)?" + p + r"$") for p in v]
             for k, v in METRIC_PATTERNS.items()}
# 比率(XBRL上は小数。0.452 = 45.2%)
PCT_METRICS = {"payout", "equity_ratio", "roe"}

# コンテキストID: CurrentYearDuration / Prior3YearInstant / ..._NonConsolidatedMember
_CTX = re.compile(r"^(CurrentYear|Prior([1-9])Year)(Duration|Instant)(_NonConsolidatedMember)?$")


def _num(s: str):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-", "－", "―", "—", "△"):
        return None
    neg = False
    if s.startswith("△") or s.startswith("▲"):
        neg, s = True, s[1:]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _fy_minus(period_end: str, k: int) -> str:
    """決算期末 'YYYY-MM-DD' から k 期前の期末(年-月)を返す。"""
    y, m = int(period_end[:4]), int(period_end[5:7])
    return f"{y - k:04d}-{m:02d}"


def parse_summary_csv(text: str, period_end: str) -> list[dict]:
    """XBRL→CSV(type=5)の本体から、主要な経営指標等の推移を期ごとに取り出す。

    返り値は期ごとの dict(古い順)。連結の値があれば連結、無ければ個別を使う
    (1株配当・配当性向は個別にしか載らないのが普通)。
    """
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = next(reader, None)
    if not header:
        return []
    h = [c.strip().strip('"') for c in header]
    try:
        i_el, i_ctx, i_unit, i_val = h.index("要素ID"), h.index("コンテキストID"), h.index("ユニットID"), h.index("値")
    except ValueError:
        return []

    # vals[(offset, consolidated?)][metric] = (priority, value)
    vals: dict[tuple[int, bool], dict[str, tuple[int, float]]] = {}
    for row in reader:
        if len(row) <= max(i_el, i_ctx, i_val):
            continue
        el = row[i_el].strip()
        if "SummaryOfBusinessResults" not in el:
            continue
        m = _CTX.match(row[i_ctx].strip())
        if not m:
            continue
        off = int(m.group(2) or 0)
        consolidated = m.group(4) is None
        v = _num(row[i_val])
        if v is None:
            continue
        for metric, pats in _COMPILED.items():
            for pri, p in enumerate(pats):
                if p.match(el):
                    if metric in PCT_METRICS:
                        unit = row[i_unit].strip().lower() if i_unit < len(row) else ""
                        # XBRL の比率は小数(unit=pure)。単位が無い場合だけ大きさで見る。
                        if unit == "pure" or (not unit and abs(v) <= 1.5):
                            v = v * 100
                    slot = vals.setdefault((off, consolidated), {})
                    if metric not in slot or pri < slot[metric][0]:
                        slot[metric] = (pri, v)
                    break
            else:
                continue
            break

    offsets = sorted({o for o, _ in vals}, reverse=True)
    out = []
    for off in offsets:
        con = vals.get((off, True), {})
        non = vals.get((off, False), {})
        rec = {"fy_end": _fy_minus(period_end, off), "consolidated": 1 if con else 0}
        for metric in METRIC_PATTERNS:
            src = con.get(metric) or non.get(metric)
            rec[metric] = round(src[1], 4) if src else None
        if any(rec[m] is not None for m in METRIC_PATTERNS):
            out.append(rec)
    return out


def parse_zip(blob: bytes, period_end: str) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith(".csv") and "/jpcrp" in ("/" + n).replace("\\", "/")]
        for n in names:
            raw = z.read(n)
            for enc in ("utf-16", "utf-8-sig", "cp932"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                continue
            recs = parse_summary_csv(text, period_end)
            if recs:
                return recs
    return []


# --- 保存まわり ---------------------------------------------------------------

def load_index() -> dict[str, dict]:
    if not INDEX_CSV.exists():
        return {}
    with INDEX_CSV.open(encoding="utf-8", newline="") as f:
        return {r["doc_id"]: r for r in csv.DictReader(f)}


def save_index(idx: dict[str, dict]) -> None:
    rows = sorted(idx.values(), key=lambda r: (r["code"], r["period_end"], r["submitted"]))
    _atomic_csv(INDEX_CSV, INDEX_FIELDS, rows)


def load_output() -> list[dict]:
    if not OUTPUT_CSV.exists():
        return []
    with OUTPUT_CSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_output(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r["code"], r["fy_end"], r["submitted"]))
    _atomic_csv(OUTPUT_CSV, OUT_FIELDS, rows)


def _atomic_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict) -> None:
    STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATE_JSON.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def universe_codes() -> set[str]:
    codes: set[str] = set()
    for p in UNIVERSE_FILES:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows = data if isinstance(data, list) else data.get("stocks", [])
        for r in rows:
            c = str(r.get("code") or r.get("ticker") or "").replace(".T", "").strip()
            if c:
                codes.add(c)
    return codes


# --- どの有報を読むか ----------------------------------------------------------

def choose_docs(docs: list[dict]) -> list[dict]:
    """直近10期をカバーする有報を、なるべく少ない通数で選ぶ。

    最新の期から順に、まだ埋まっていない期を含む有報のうち一番古いもの
    (=最も過去まで届くもの)を取る。毎年有報が出ていれば、最新と5年前の2通になる。
    """
    if not docs:
        return []
    docs = sorted(docs, key=lambda d: (d["period_end"], d["submitted"]))
    # 同じ決算期の有報が複数ある場合(提出し直し等)は新しい提出を使う
    by_pe: dict[str, dict] = {}
    for d in docs:
        by_pe[d["period_end"][:7]] = d
    latest_pe = max(by_pe)
    window = [_fy_minus(latest_pe + "-01", k) for k in range(WINDOW_FY)]
    cover = {pe: {_fy_minus(pe + "-01", k) for k in range(5)} for pe in by_pe}
    need = set(window)
    chosen = []
    while need:
        f = max(need)
        cands = [pe for pe, cv in cover.items() if f in cv]
        if not cands:
            need.discard(f)
            continue
        pe = min(cands)
        chosen.append(by_pe[pe])
        need -= cover[pe]
    return chosen


# --- 通信 ---------------------------------------------------------------------

class Api:
    def __init__(self, key: str):
        self.key = key
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "dividend-screener/1.0"

    def _get(self, url: str, params: dict) -> requests.Response | None:
        params = dict(params, **{"Subscription-Key": self.key})
        for attempt in range(4):
            try:
                r = self.s.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"  通信エラー({type(e).__name__}) 再試行 {attempt + 1}", file=sys.stderr)
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 200:
                time.sleep(SLEEP_SEC)
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise SystemExit("EDINET APIキーが無効です(401/403)。GitHub の Secrets を確認してください。")
            return None
        return None

    def list_asr(self, d: date) -> list[dict] | None:
        r = self._get(f"{API}/documents.json", {"date": d.isoformat(), "type": 2})
        if r is None:
            return None
        try:
            js = r.json()
        except ValueError:
            return None
        if str(js.get("metadata", {}).get("status")) not in ("200", ""):
            # 401 はステータスコードではなく本文で返ってくることがある
            if str(js.get("statusCode") or js.get("metadata", {}).get("status")) == "401":
                raise SystemExit("EDINET APIキーが無効です。GitHub の Secrets を確認してください。")
            return None
        out = []
        for x in js.get("results") or []:
            if (x.get("docTypeCode") == "120" and x.get("secCode")
                    and x.get("periodEnd") and str(x.get("withdrawalStatus", "0")) == "0"
                    and str(x.get("csvFlag", "1")) == "1"):
                sec = str(x["secCode"]).strip()
                code = sec[:4] if len(sec) == 5 else sec
                out.append({"code": code, "doc_id": x["docID"],
                            "period_end": x["periodEnd"],
                            "submitted": (x.get("submitDateTime") or d.isoformat())[:10]})
        return out

    def get_csv_zip(self, doc_id: str) -> bytes | None:
        r = self._get(f"{API}/documents/{doc_id}", {"type": 5})
        if r is None:
            return None
        if "zip" not in r.headers.get("Content-Type", "zip") and not r.content[:2] == b"PK":
            return None
        return r.content


# --- 本体 ---------------------------------------------------------------------

def main() -> int:
    key = os.getenv("EDINET_API_KEY", "").strip()
    if not key:
        print("EDINET_API_KEY が未設定のため、10年実績データの取得をスキップします。")
        return 0
    t0 = time.time()
    over = lambda: time.time() - t0 > TIME_BUDGET_SEC  # noqa: E731
    api = Api(key)
    st = load_state()
    idx = load_index()

    # 1) 書類一覧の走査。まず新しい側(前回の続き〜昨日)、次に古い側へ広げる。
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    target_from = today - timedelta(days=int(SCAN_YEARS * 365.25))
    s_from = date.fromisoformat(st["scanned_from"]) if st.get("scanned_from") else None
    s_thru = date.fromisoformat(st["scanned_through"]) if st.get("scanned_through") else None
    if s_from is None or s_thru is None:
        # まだ何も走査していない: 昨日から過去へ向かって走査を始める
        s_from, s_thru = today, today - timedelta(days=1)

    n_list = 0
    d = s_thru + timedelta(days=1)
    while d < today and not over():
        res = api.list_asr(d)
        if res is None:
            print(f"  {d} の書類一覧を取得できず。次回に回します。", file=sys.stderr)
            break
        for x in res:
            idx[x["doc_id"]] = x
        s_thru = d
        d += timedelta(days=1)
        n_list += 1
    d = s_from - timedelta(days=1)
    while d >= target_from and not over():
        res = api.list_asr(d)
        if res is None:
            print(f"  {d} の書類一覧を取得できず。次回に回します。", file=sys.stderr)
            break
        for x in res:
            idx[x["doc_id"]] = x
        s_from = d
        d -= timedelta(days=1)
        n_list += 1
        if n_list % 200 == 0:
            st.update(scanned_from=s_from.isoformat(), scanned_through=s_thru.isoformat())
            save_index(idx)
            save_state(st)
            print(f"  書類一覧 {n_list}日分 走査(〜{s_from})  有報 {len(idx)}通", flush=True)
    st.update(scanned_from=s_from.isoformat(), scanned_through=s_thru.isoformat())
    save_index(idx)
    save_state(st)
    print(f"書類一覧: {n_list}日分を走査。走査済み {s_from}〜{s_thru}、有報 {len(idx)}通", flush=True)

    # 2) 銘柄ごとに読むべき有報を決め、未読のものだけ読む
    uni = universe_codes()
    by_code: dict[str, list[dict]] = {}
    for x in idx.values():
        if uni and x["code"] not in uni:
            continue
        by_code.setdefault(x["code"], []).append(x)

    out_rows = load_output()
    done = {r["doc_id"] for r in out_rows}
    done |= set(st.get("empty_docs", []))      # 読んだが指標が無かった有報
    todo_latest, todo_old = [], []
    for code, docs in by_code.items():
        chosen = choose_docs(docs)
        for i, dmeta in enumerate(chosen):
            if dmeta["doc_id"] not in done:
                (todo_latest if i == 0 else todo_old).append(dmeta)
    # 全銘柄の最新をまず埋め、そのあと古い有報。途中で時間切れでも
    # 「直近5期だけある銘柄」が増える方が、一部の銘柄だけ10期そろうより役に立つ。
    todo = todo_latest + todo_old
    print(f"読む有報: {len(todo)}通(最新 {len(todo_latest)} / 過去 {len(todo_old)})", flush=True)

    empty = set(st.get("empty_docs", []))
    n_ok = n_fail = 0
    for i, dmeta in enumerate(todo, 1):
        if over():
            print("時間の上限に達したので、残りは次回に回します。", flush=True)
            break
        blob = api.get_csv_zip(dmeta["doc_id"])
        if blob is None:
            n_fail += 1
            continue
        try:
            recs = parse_zip(blob, dmeta["period_end"])
        except zipfile.BadZipFile:
            recs = []
        if not recs:
            empty.add(dmeta["doc_id"])
        for r in recs:
            r.update(code=dmeta["code"], doc_id=dmeta["doc_id"], submitted=dmeta["submitted"])
            out_rows.append(r)
        n_ok += 1
        if i % SAVE_EVERY == 0:
            st["empty_docs"] = sorted(empty)
            save_output(out_rows)
            save_state(st)
            print(f"  {i}/{len(todo)} 通 処理", flush=True)

    # 読む対象から外れた古い有報の行は落とす(10期より古いデータを残さない)
    keep_docs = {d["doc_id"] for docs in by_code.values() for d in choose_docs(docs)}
    if keep_docs:
        out_rows = [r for r in out_rows if r["doc_id"] in keep_docs]
    st["empty_docs"] = sorted(empty & keep_docs) if keep_docs else sorted(empty)
    st["remaining_docs"] = max(0, len(todo) - n_ok - n_fail)
    st["updated_at"] = datetime.now(jst).isoformat(timespec="seconds")
    save_output(out_rows)
    save_state(st)
    print(f"完了: 読んだ有報 {n_ok}通 / 失敗 {n_fail}通 / 残り {st['remaining_docs']}通 / "
          f"財務データ {len({r['code'] for r in out_rows})}銘柄", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
