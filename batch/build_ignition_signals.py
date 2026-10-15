"""「燃料」と「着火スイッチ」の点灯状況をまとめる。

燃料(買い戻しが起きる土台) 2項目と、着火スイッチ(揃うほど相場の終了が近い) 8項目を
1つのJSONにまとめ、web/ignition.html が表示する。

自動で判定するもの:
  燃料①  二市場(東証・名証)の売り残 ≥ 8,000億円      … margin_balance_history.json (JPX 週次)
  燃料②  日経レバ(1570)の信用倍率 < 1倍              … Yahoo!ファイナンスの銘柄ページ (週次)
  着火②  二市場の売り残 < 8,000億円                   … 同上
  着火③  日経レバの信用倍率 ≥ 1倍                     … 同上
  着火④  日本株の優位性低下(暫定ロジック)            … indices.json の日経225(ドル換算)とNASDAQ100
  着火⑤  日経平均 < 理論値(EPS × 基準PER)            … nikkei_eps_history.json
  着火⑥  海外投資家の日本株売り(直近4週の合計が売り越し)… investor_flows_jp.json (JPX 週次)
  着火⑦  VIXのMACDがゴールデンクロス中                 … yfinance ^VIX
  着火⑧  日経平均がPBR2倍(BPS×2)に到達               … 日経の公表PBR(加重平均)

手動で判定するもの:
  着火①  BW(転換社債型新株予約権付社債)の終了サイン
  ほかの項目も data/ignition_manual.json の status で上書きできる。

Output: web/data/ignition_signals.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from curl_cffi import requests as _http
    _SESSION = _http.Session(impersonate="chrome")
except ImportError:
    import urllib.request
    _SESSION = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DATA = REPO_ROOT / "web" / "data"
OUTPUT_PATH = WEB_DATA / "ignition_signals.json"
MANUAL_PATH = REPO_ROOT / "data" / "ignition_manual.json"

JST = timezone(timedelta(hours=9))

SHORT_SALES_LINE_OKU = 8000          # 二市場の売り残の境目(億円)
SHORT_SALES_SWITCH_NOTE = ("売り残が多いほど、売り方の買い戻しで相場は上昇しやすい。"
                           "8,000億円を割ると買い戻しが一巡し、上昇の燃料が切れたと見る。")
LEV_ETF_CODE = "1570"               # NEXT FUNDS 日経平均レバレッジ・インデックス連動型
LEV_URL = f"https://finance.yahoo.co.jp/quote/{LEV_ETF_CODE}.T"
PBR_URL = "https://indexes.nikkei.co.jp/nkave/archives/data?list=pbr"
DATALOAD_URL = "https://indexes.nikkei.co.jp/nkave/statistics/dataload"
PBR_CEILING = 2.0
EDGE_DAYS = 63                      # 日本株の優位性: 約3ヶ月の騰落率で比べる
FLOW_WEEKS = 4                      # 海外投資家: 直近4週の合計で見る


def _get_text(url: str) -> str:
    if _SESSION is not None:
        return _SESSION.get(url, timeout=25).text
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 日経レバ(1570) 信用倍率 — Yahoo!ファイナンスの銘柄ページに埋め込まれたJSONから読む
# ---------------------------------------------------------------------------

def _yahoo_item(html: str, key: str) -> tuple[str | None, str | None]:
    # 埋め込みJSONは \" でエスケープされている。key の直後が引用符のものだけを拾う
    # (marginTransactionBuy と marginTransactionBuyChange を区別するため)。
    m = re.search(
        key + r'\\*"' + r'.{0,600}?"?value\\*":\\*"([^"\\]+)\\*".{0,300}?updateDate\\*":\\*"(\d{1,2}/\d{1,2})',
        html, re.DOTALL)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _resolve_md(md: str, today: datetime) -> str:
    mo, dd = (int(x) for x in md.split("/"))
    y = today.year
    if (mo, dd) > (today.month, today.day):
        y -= 1
    return f"{y:04d}-{mo:02d}-{dd:02d}"


def fetch_lev_margin(today: datetime) -> dict | None:
    html = _get_text(LEV_URL)
    ratio_s, md = _yahoo_item(html, "marginCreditMagnification")
    buy_s, _ = _yahoo_item(html, "marginTransactionBuy")
    sell_s, _ = _yahoo_item(html, "marginTransactionSell")
    if md is None:
        return None
    buy, sell = _num(buy_s), _num(sell_s)
    ratio = _num(ratio_s)
    if ratio is None and buy is not None and sell:
        ratio = round(buy / sell, 2)
    if ratio is None:
        return None
    return {"date": _resolve_md(md, today), "ratio": ratio, "buy": buy, "sell": sell}


# ---------------------------------------------------------------------------
# 日経平均の公表PBR(加重平均) — PERと同じ表の形式
# ---------------------------------------------------------------------------

def _parse_ratio_rows(html: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.DOTALL)]
        cells = [c for c in cells if c]
        if len(cells) < 2 or not re.match(r"^\d{4}\.\d{2}\.\d{2}$", cells[0]):
            continue
        v = _num(cells[1])
        if v:
            out[cells[0].replace(".", "-")] = v
    return out


def fetch_pbr_history(existing: dict[str, float], today: datetime, months: int = 13) -> dict[str, float]:
    import time
    hist = dict(existing)
    try:
        hist.update(_parse_ratio_rows(_get_text(PBR_URL)))
    except Exception as e:
        print(f"  WARN PBR(当月) 取得失敗: {e}", file=sys.stderr)
    y, m = today.year, today.month
    for _ in range(months):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        key = f"{y:04d}-{m:02d}"
        if sum(1 for d in hist if d.startswith(key)) >= 5:
            continue
        try:
            got = _parse_ratio_rows(_get_text(f"{DATALOAD_URL}?list=pbr&year={y}&month={m}"))
            hist.update({d: v for d, v in got.items() if d.startswith(key)})
            print(f"  PBR backfill {key}: {len(got)} rows", file=sys.stderr)
        except Exception as e:
            print(f"  WARN PBR backfill {key} 失敗: {e}", file=sys.stderr)
        time.sleep(0.8)
    cutoff = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    return {d: v for d, v in sorted(hist.items()) if d >= cutoff}


# ---------------------------------------------------------------------------
# VIX の MACD
# ---------------------------------------------------------------------------

def _ema(vals: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def fetch_vix_macd() -> list[dict]:
    import yfinance as yf
    t = yf.Ticker("^VIX", session=_SESSION) if _SESSION is not None else yf.Ticker("^VIX")
    df = t.history(period="1y", interval="1d", auto_adjust=False)
    if df.empty:
        return []
    dates = [i.date().isoformat() for i in df.index]
    closes = [float(c) for c in df["Close"]]
    macd = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    sig = _ema(macd, 9)
    return [{"date": d, "close": round(c, 2), "macd": round(m, 3), "signal": round(s, 3)}
            for d, c, m, s in zip(dates, closes, macd, sig)]


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def _item(key, no, name, lit, value, threshold, asof, source, method="auto", detail="", chart=None):
    return {
        "key": key, "no": no, "name": name,
        "lit": lit,                      # True=点灯 / False=消灯 / None=未判定
        "value": value, "threshold": threshold, "asof": asof,
        "source": source, "method": method, "detail": detail,
        "chart": chart,
    }


def _apply_manual(item: dict, manual: dict) -> dict:
    m = (manual.get("items") or {}).get(item["key"]) or {}
    if m.get("status") in (True, False):
        item["auto_lit"] = item["lit"]
        item["lit"] = m["status"]
        item["method"] = "manual"
        item["manual_note"] = m.get("note", "")
        item["manual_updated"] = m.get("updated", "")
    elif item["method"] == "manual":
        item["manual_note"] = m.get("note", "")
        item["manual_updated"] = m.get("updated", "")
    return item


def main() -> int:
    now = datetime.now(JST)
    prev = _load(OUTPUT_PATH)
    manual = _load(MANUAL_PATH)
    theory_per = float(manual.get("theory_per") or 16.0)

    # ---- 二市場の売り残 -------------------------------------------------
    mb = _load(WEB_DATA / "margin_balance_history.json")
    mh = [h for h in mb.get("history", []) if h.get("sales_value_mil")]
    sales_series = [[h["date"], round(h["sales_value_mil"] / 100)] for h in mh[-52:]]   # 百万円 → 億円
    sales = sales_series[-1] if sales_series else None

    # ---- 日経レバ 信用倍率(週次・履歴を貯める) ----------------------
    lev_hist = {h["date"]: h for h in (prev.get("lev_margin_history") or [])}
    try:
        got = fetch_lev_margin(now)
        if got:
            lev_hist[got["date"]] = got
            print(f"  1570 信用倍率 {got['ratio']} ({got['date']})", file=sys.stderr)
        else:
            print("  WARN 1570 の信用倍率をページから読めませんでした", file=sys.stderr)
    except Exception as e:
        print(f"  WARN 1570 取得失敗: {e}", file=sys.stderr)
    lev_list = [lev_hist[d] for d in sorted(lev_hist)][-104:]
    lev = lev_list[-1] if lev_list else None

    # ---- 日経平均: 終値・EPS・PER・PBR ---------------------------------
    eps_doc = _load(WEB_DATA / "nikkei_eps_history.json")
    eh = eps_doc.get("history", [])
    closes = {h["date"]: h["close"] for h in eh if h.get("close")}
    eps_rows = [h for h in eh if h.get("eps_weighted")]
    last_close_date = max(closes) if closes else None
    last_close = closes.get(last_close_date) if last_close_date else None
    last_eps = eps_rows[-1] if eps_rows else None

    pbr_prev = {h["date"]: h["pbr"] for h in (prev.get("pbr_history") or [])}
    try:
        pbr_hist = fetch_pbr_history(pbr_prev, now)
    except Exception as e:
        print(f"  WARN PBR 取得失敗: {e}", file=sys.stderr)
        pbr_hist = pbr_prev
    pbr_dates = sorted(d for d in pbr_hist if d in closes)
    last_pbr_date = pbr_dates[-1] if pbr_dates else None
    bps = round(closes[last_pbr_date] / pbr_hist[last_pbr_date], 1) if last_pbr_date else None

    # 日経平均・理論値・PBR2倍ラインの推移(過去1年)
    cutoff = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    nk_chart, last_eps_v, last_bps_v = [], None, None
    eps_by_date = {h["date"]: h["eps_weighted"] for h in eps_rows}
    for d in sorted(closes):
        if d in eps_by_date:
            last_eps_v = eps_by_date[d]
        if d in pbr_hist:
            last_bps_v = closes[d] / pbr_hist[d]
        if d < cutoff:
            continue
        nk_chart.append([
            d, round(closes[d]),
            round(last_eps_v * theory_per) if last_eps_v else None,
            round(last_bps_v * PBR_CEILING) if last_bps_v else None,
        ])

    # ---- 海外投資家 ----------------------------------------------------
    fl = _load(WEB_DATA / "investor_flows_jp.json")
    fh = [h for h in fl.get("history", []) if (h.get("flows") or {}).get("foreign")]
    flow_series = [[h["date"], h["flows"]["foreign"]["net_oku"]] for h in fh[-26:]]
    last4 = [v for _, v in flow_series[-FLOW_WEEKS:]]
    flow_sum = round(sum(last4), 1) if len(last4) == FLOW_WEEKS else None

    # ---- 日本株の優位性(暫定): 日経225(ドル換算) vs NASDAQ100 の3ヶ月騰落率 ----
    idx = {i["id"]: i for i in _load(WEB_DATA / "indices.json").get("indices", [])}
    edge = None
    try:
        nk = {r[0]: r[4] for r in idx["N225"]["history"]}
        ndx = {r[0]: r[4] for r in idx["NDX"]["history"]}
        fx = {r[0]: r[4] for r in idx["USDJPY"]["history"]}
        common = sorted(set(nk) & set(ndx) & set(fx))
        if len(common) > EDGE_DAYS:
            series = []
            for i in range(EDGE_DAYS, len(common)):
                a, b = common[i - EDGE_DAYS], common[i]
                jp_usd = (nk[b] / fx[b]) / (nk[a] / fx[a]) - 1
                us = ndx[b] / ndx[a] - 1
                series.append([b, round((jp_usd - us) * 100, 2)])
            edge = {"date": common[-1], "spread": series[-1][1], "series": series[-250:]}
    except (KeyError, IndexError, ZeroDivisionError, TypeError) as e:
        print(f"  WARN 日本株の優位性を計算できませんでした: {e}", file=sys.stderr)

    # ---- VIX MACD ------------------------------------------------------
    try:
        vix = fetch_vix_macd()
    except Exception as e:
        print(f"  WARN VIX 取得失敗: {e}", file=sys.stderr)
        vix = prev.get("vix_macd") or []
    vix_last = vix[-1] if vix else None
    cross_date = None
    if vix:
        for i in range(len(vix) - 1, 0, -1):
            above, was_above = vix[i]["macd"] > vix[i]["signal"], vix[i - 1]["macd"] > vix[i - 1]["signal"]
            if above != was_above:
                cross_date = vix[i]["date"]
                break

    # ---- 項目を組み立てる ----------------------------------------------
    jpx_margin = "JPX 信用取引現在高(二市場計・週次)"
    lev_src = f"Yahoo!ファイナンス {LEV_ETF_CODE} 信用取引情報(週次)"

    fuel = [
        _item("fuel_short_sales", 1, "二市場の売り残が8,000億円以上",
              (sales[1] >= SHORT_SALES_LINE_OKU) if sales else None,
              {"num": sales[1], "unit": "億円"} if sales else None,
              f"≥ {SHORT_SALES_LINE_OKU:,}億円", sales[0] if sales else None, jpx_margin,
              detail="売り方の買い戻し余力。多いほど踏み上げの燃料になる。",
              chart={"type": "line", "series": sales_series, "line": SHORT_SALES_LINE_OKU, "unit": "億円"}),
        _item("fuel_lev_ratio", 2, "日経レバの信用倍率が1倍未満",
              (lev["ratio"] < 1) if lev else None,
              {"num": lev["ratio"], "unit": "倍"} if lev else None,
              "< 1.00倍", lev["date"] if lev else None, lev_src,
              detail="日経レバ(1570)で売りが買いを上回る状態。",
              chart={"type": "line", "series": [[h["date"], h["ratio"]] for h in lev_list], "line": 1, "unit": "倍"}),
    ]

    switches = [
        _item("bw_end", 1, "BW(転換社債型新株予約権付社債)の終了サイン", None, None,
              "手動判定", None, "手動入力", method="manual",
              detail="定性的な判断のため、data/ignition_manual.json で点灯/消灯を入力する。"),
        _item("short_sales_below", 2, "二市場の売り残が8,000億円未満(買い戻し一巡)",
              (sales[1] < SHORT_SALES_LINE_OKU) if sales else None,
              {"num": sales[1], "unit": "億円"} if sales else None,
              f"< {SHORT_SALES_LINE_OKU:,}億円", sales[0] if sales else None, jpx_margin,
              detail=SHORT_SALES_SWITCH_NOTE,
              chart={"type": "line", "series": sales_series, "line": SHORT_SALES_LINE_OKU, "unit": "億円"}),
        _item("lev_ratio_above", 3, "日経レバの信用倍率が1倍以上",
              (lev["ratio"] >= 1) if lev else None,
              {"num": lev["ratio"], "unit": "倍"} if lev else None,
              "≥ 1.00倍", lev["date"] if lev else None, lev_src,
              chart={"type": "line", "series": [[h["date"], h["ratio"]] for h in lev_list], "line": 1, "unit": "倍"}),
        _item("japan_edge", 4, "日本株の優位性低下",
              (edge["spread"] < 0) if edge else None,
              {"num": edge["spread"], "unit": "%pt", "signed": True} if edge else None,
              "< 0%pt(暫定)", edge["date"] if edge else None,
              "日経225(ドル換算)とNASDAQ100の3ヶ月騰落率の差",
              detail="暫定ロジック: 直近約3ヶ月で日経225(ドル換算)がNASDAQ100に負けていれば点灯。"
                     "見方が違う場合は data/ignition_manual.json で上書きできる。",
              chart={"type": "line", "series": edge["series"], "line": 0, "unit": "%pt"} if edge else None),
        _item("below_theory", 5, "日経平均が理論値(PER×EPS)を割り込む",
              (last_close < last_eps["eps_weighted"] * theory_per) if (last_close and last_eps) else None,
              {"num": round(last_close), "unit": "円",
               "sub": f"理論値 {round(last_eps['eps_weighted'] * theory_per):,}円"
                      f"(EPS {last_eps['eps_weighted']:,.0f}円 × {theory_per:g}倍)"} if (last_close and last_eps) else None,
              f"終値 < EPS×{theory_per:g}倍", last_close_date, "日経平均 終値・加重平均EPS(日経公表PERから算出)",
              detail=f"理論値の基準PERは {theory_per:g}倍(data/ignition_manual.json の theory_per で変更できる)。"
                     "16/17/18倍の3本は下の「日経平均とEPS×PER」グラフで見られる。",
              chart={"type": "nikkei", "series": nk_chart, "show": "theory"}),
        _item("foreign_selling", 6, "海外投資家の日本株売り",
              (flow_sum < 0) if flow_sum is not None else None,
              {"num": flow_sum, "unit": "億円", "signed": True,
               "sub": f"直近{FLOW_WEEKS}週の合計(最新週 {flow_series[-1][1]:+,.0f}億円)"} if flow_sum is not None else None,
              f"直近{FLOW_WEEKS}週合計 < 0", flow_series[-1][0] if flow_series else None,
              "JPX 投資部門別売買状況(東証プライム・週次)",
              chart={"type": "bar", "series": flow_series, "line": 0, "unit": "億円"}),
        _item("vix_macd_gc", 7, "VIXのMACDがゴールデンクロス",
              (vix_last["macd"] > vix_last["signal"]) if vix_last else None,
              {"num": vix_last["close"], "unit": "",
               "sub": f"MACD {vix_last['macd']:+.2f} / シグナル {vix_last['signal']:+.2f}"
                      + (f"・{cross_date} にクロス" if cross_date else "")} if vix_last else None,
              "MACD > シグナル", vix_last["date"] if vix_last else None, "VIX日足(Yahoo Finance)・MACD(12,26,9)",
              detail="MACDがシグナルを上抜けている間を点灯とする。",
              chart={"type": "macd", "series": [[v["date"], v["macd"], v["signal"]] for v in vix[-130:]]} if vix else None),
        _item("pbr_ceiling", 8, "PBR2倍(BPS×2)に到達",
              (closes[last_pbr_date] >= bps * PBR_CEILING) if bps else None,
              {"num": pbr_hist[last_pbr_date], "unit": "倍",
               "sub": f"PBR2倍 = {round(bps * PBR_CEILING):,}円(BPS {bps:,.0f}円)"} if bps else None,
              "PBR ≥ 2.00倍", last_pbr_date, "日経公表 日経平均PBR(加重平均)",
              detail="BPS = 終値 ÷ PBR。PBR2倍ラインが天井の目安。",
              chart={"type": "nikkei", "series": nk_chart, "show": "pbr"}),
    ]

    fuel = [_apply_manual(i, manual) for i in fuel]
    switches = [_apply_manual(i, manual) for i in switches]

    out = {
        "generated_at_jst": now.isoformat(),
        "theory_per": theory_per,
        "summary": {
            "fuel_lit": sum(1 for i in fuel if i["lit"] is True), "fuel_total": len(fuel),
            "switch_lit": sum(1 for i in switches if i["lit"] is True), "switch_total": len(switches),
            "switch_unknown": sum(1 for i in switches if i["lit"] is None),
        },
        "fuel": fuel,
        "switches": switches,
        # 次回以降に引き継ぐ履歴(Yahooは最新値しか出ないので、週ごとに貯める)
        "lev_margin_history": lev_list,
        "pbr_history": [{"date": d, "pbr": v} for d, v in sorted(pbr_hist.items())],
        "vix_macd": vix[-130:],
        # 日経平均とEPS×PER(16/17/18倍)・EPS・PERの推移グラフ用。[日付, 終値, EPS, PER]
        "nikkei_valuation": [[h["date"], round(h["close"]), round(h["eps_weighted"], 1), h["per_weighted"]]
                             for h in eps_rows if h.get("close") and h.get("per_weighted")],
    }
    OUTPUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    s = out["summary"]
    print(f"燃料 {s['fuel_lit']}/{s['fuel_total']} ・ 着火スイッチ {s['switch_lit']}/{s['switch_total']}"
          f"(未判定 {s['switch_unknown']}) → {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
