#!/usr/bin/env python3
"""IPO銘柄の会社プロフィールを取得してキャッシュする。

yfinance の Ticker.info から、事業内容・セクター・時価総額・従業員数などを
1銘柄ずつ取る。

**毎日取り直さない。**
会社概要はほとんど変わらないのに、Ticker.info は1銘柄1リクエストで遅く、
GitHub Actions のIPからだとレート制限にも当たりやすい。取得済みの銘柄は
飛ばし、1回の実行で新規ぶんを上限まで埋める。数日かければ全銘柄が揃う。

**時価総額だけは変わるので毎回更新する。**
と言いたいところだが、そのために全銘柄を叩くと結局同じことになる。
時価総額は「取得した時点の値」と割り切り、いつ取ったかを添える。
株価は build_ipo_page.py が毎日更新するので、鮮度が要るのはそちらで足りる。

Input:  data/ipo_jp.json
Output: data/ipo_profiles_jp.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
IPO_LIST = REPO_ROOT / "data" / "ipo_jp.json"
OUT_PATH = REPO_ROOT / "data" / "ipo_profiles_jp.json"

JST = timezone(timedelta(hours=9))

# 1回の実行で新しく取りに行く上限。全件を一度に叩くとレート制限に当たる。
MAX_NEW = int(os.getenv("IPO_PROFILE_MAX_NEW", "50"))
SLEEP = float(os.getenv("IPO_PROFILE_SLEEP", "0.4"))

# 取得できなかった銘柄を毎日叩き直さないための待ち日数。
# 上場直後はYahoo側にまだ情報が無いことがあるので、少し待って再挑戦する。
RETRY_AFTER_DAYS = 7

FIELDS = {
    "summary_en": "longBusinessSummary",
    "sector_en": "sector",
    "industry_en": "industry",
    "employees": "fullTimeEmployees",
    "website": "website",
    "city": "city",
    "market_cap": "marketCap",
}

# yfinance のセクターは英語。画面は日本語なので対訳を持つ。
# 未知のものは英語のまま出す（勝手な訳を作るより、原文の方が調べられる）。
SECTOR_JA = {
    "Technology": "情報技術",
    "Communication Services": "通信サービス",
    "Consumer Cyclical": "一般消費財",
    "Consumer Defensive": "生活必需品",
    "Financial Services": "金融",
    "Healthcare": "ヘルスケア",
    "Industrials": "資本財・サービス",
    "Basic Materials": "素材",
    "Energy": "エネルギー",
    "Utilities": "公益",
    "Real Estate": "不動産",
}


def load_cache() -> dict:
    if not OUT_PATH.exists():
        return {"updated_at": None, "profiles": {}}
    try:
        d = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 壊れていても消さない。読めないなら空から作り直す方が安全だが、
        # 既存の内容を失うので、ここでは止めて人間に気付かせる。
        raise
    d.setdefault("profiles", {})
    return d


def needs_fetch(code: str, cache: dict, today) -> bool:
    p = cache["profiles"].get(code)
    if not p:
        return True
    if p.get("ok"):
        return False
    # 失敗した銘柄は一定期間おいて再挑戦する
    try:
        last = datetime.fromisoformat(p.get("checked_at", "")).date()
    except ValueError:
        return True
    return (today - last).days >= RETRY_AFTER_DAYS


def fetch_one(code: str) -> dict:
    info = yf.Ticker(f"{code}.T").info or {}
    out = {k: info.get(src) for k, src in FIELDS.items()}
    if not out.get("summary_en") and not out.get("sector_en"):
        return {"ok": False}
    out["sector_ja"] = SECTOR_JA.get(out.get("sector_en") or "", out.get("sector_en"))
    out["ok"] = True
    return out


def main() -> int:
    if not IPO_LIST.exists():
        print(f"{IPO_LIST} がありません。", file=sys.stderr)
        return 0

    codes = [r["code"] for r in json.loads(IPO_LIST.read_text(encoding="utf-8")).get("ipos", [])]
    cache = load_cache()
    today = datetime.now(JST).date()
    checked_at = today.isoformat()

    todo = [c for c in codes if needs_fetch(c, cache, today)][:MAX_NEW]
    print(f"対象 {len(codes)} 銘柄 / 今回取得 {len(todo)} 銘柄", file=sys.stderr)

    ok = fail = 0
    for i, code in enumerate(todo, 1):
        try:
            rec = fetch_one(code)
        except Exception as e:
            print(f"  {code}: {e}", file=sys.stderr)
            rec = {"ok": False}
        rec["checked_at"] = checked_at
        cache["profiles"][code] = rec
        ok += 1 if rec.get("ok") else 0
        fail += 0 if rec.get("ok") else 1
        if i % 10 == 0:
            print(f"  {i}/{len(todo)}", file=sys.stderr)
        time.sleep(SLEEP)

    cache["updated_at"] = datetime.now(JST).isoformat(timespec="seconds")
    cache["count"] = len(cache["profiles"])
    cache["ok_count"] = sum(1 for p in cache["profiles"].values() if p.get("ok"))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    print(f"成功 {ok} / 失敗 {fail}（累計 {cache['ok_count']}/{cache['count']}）",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
