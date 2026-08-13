#!/usr/bin/env python3
"""連続増配期数を算出して dividend_screener.json に足す。

日本株でこれを正しく出すには、罠が4つある。

**1. 暦年で合計してはいけない。**
yfinance が返すのは中間配当と期末配当の個別支払い。3月期決算なら
FY2025（2025年4月〜2026年3月）の配当は 2025年12月（中間）と 2026年6月（期末）に
払われる。暦年でまとめると、ある年の期末と翌期の中間が同じ袋に入り、
増配していない会社が増配に見えたりその逆が起きる。

**2. 進行中の期を入れてはいけない。**
まだ中間しか払っていない期を1年分として数えると、**ほぼ全銘柄が減配判定になる**。
最新の期は、期末配当の支払月を過ぎるまで「未確定」として除外する。

**3. 入力は「権利落ち日」であって支払日ではない。**
yfinance の `.dividends` が返すのは権利落ち日。3月期なら中間が9月末、期末が3月末で、
どちらも当該会計年度に収まる。もし支払日（中間12月・期末翌年6月）を渡すと、
期末分が翌年度のバケツに落ちて集計が1期ずれる。実装中にこれで一度間違えた。

**4. 記念配当は増配ではない。**
創立◯周年などの一過性の上乗せは翌期に剝落し、連続記録を切る。実際に減っている
のだから記録が切れるのは正しいが、「記念配当を除けば連続増配」という会社を
一律に切り捨てることにもなる。ここは判定を変えず、`had_special` を立てて
画面側で注釈できるようにする（自動で除外すると、本物の減配を隠す方が危険）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

# 3月期決算を既定にする。日本の上場企業の約7割が該当。
FY_END_MONTH = int(os.getenv("FY_END_MONTH", "3"))

# 期末配当が実際に振り込まれるまでの猛予（月）。3月期なら6月頃。
SETTLE_MONTHS = int(os.getenv("DIV_SETTLE_MONTHS", "3"))

# この比率未満の増加は「据え置き」とみなす（端数調整・為替差などを吸収）
INCREASE_EPS = float(os.getenv("DIV_INCREASE_EPS", "0.005"))

# 記念配当とみなす一過性の突出（前期比でこの倍率を超え、翌期に戻る）
SPECIAL_RATIO = float(os.getenv("DIV_SPECIAL_RATIO", "1.5"))


@dataclass
class Streak:
    years: int                 # 連続増配期数（据え置きで途切れる）
    non_decreasing: int        # 連続非減配期数（据え置きは継続とみなす）
    latest_fy: int | None      # 判定に使った直近の確定期
    latest_dps: float | None
    history: list[dict]        # [{fy, dps}] 古い順
    had_special: bool          # 一過性の突出を含むか
    complete_years: int        # 確定した期の数（少ないと判定の信頼度が低い）
    capped: bool               # 取得できた全期間で増配 = 実際の記録はさらに長い可能性


def fiscal_year(ts: pd.Timestamp, fy_end_month: int = FY_END_MONTH) -> int:
    """その支払いがどの会計年度に属するか。

    3月期の場合、2026年1月の支払いは FY2025 に属する。
    「年度の開始年」を FY 番号として返す。
    """
    return ts.year if ts.month > fy_end_month else ts.year - 1


def _annual_dps(dividends: pd.Series, fy_end_month: int) -> pd.Series:
    """支払い明細を会計年度ごとに合計する。"""
    if dividends is None or len(dividends) == 0:
        return pd.Series(dtype="float64")
    s = dividends.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    fy = s.index.map(lambda t: fiscal_year(pd.Timestamp(t), fy_end_month))
    return s.groupby(fy).sum().sort_index()


def _settled_by(fy: int, fy_end_month: int) -> pd.Timestamp:
    """会計年度 fy の期末配当が支払い終わったとみなせる時点。

    FY2025（3月期）は 2026年3月末に締まり、期末配当は6月頃に支払われる。
    """
    month = fy_end_month + SETTLE_MONTHS
    year = fy + 1 + (month - 1) // 12
    return pd.Timestamp(year=year, month=(month - 1) % 12 + 1, day=1)


def _drop_incomplete(annual: pd.Series, fy_end_month: int, now: pd.Timestamp) -> pd.Series:
    """期末配当の支払時期を過ぎていない年度を落とす。

    これを怠ると、中間配当しか出ていない進行中の期が「半減」として扱われ、
    連続増配記録がほぼ全社で途切れる。
    """
    if annual.empty:
        return annual
    keep = [fy for fy in annual.index if _settled_by(int(fy), fy_end_month) <= now]
    return annual[annual.index.isin(keep)]


def compute_streak(
    dividends: pd.Series,
    fy_end_month: int = FY_END_MONTH,
    now: pd.Timestamp | None = None,
) -> Streak:
    now = now or pd.Timestamp.utcnow().tz_localize(None)
    annual = _drop_incomplete(_annual_dps(dividends, fy_end_month), fy_end_month, now)

    if annual.empty:
        return Streak(0, 0, None, None, [], False, 0, False)

    vals = annual.tolist()
    fys = annual.index.tolist()

    def count_back(predicate) -> int:
        """直近の期から遡って、predicate(前期, 当期) が成り立ち続けた期数。"""
        n = 0
        for i in range(len(vals) - 1, 0, -1):
            prev, cur = vals[i - 1], vals[i]
            if prev <= 0 or not predicate(prev, cur):
                break
            n += 1
        return n

    # 増配 = 前期比で明確に増えた。据え置きは含めない。
    streak = count_back(lambda p, c: c / p > 1 + INCREASE_EPS)
    # 非減配 = 減っていない。据え置きを含む。
    non_dec = count_back(lambda p, c: c / p >= 1 - INCREASE_EPS)

    had_special = False
    for i in range(1, len(vals) - 1):
        if vals[i - 1] > 0 and vals[i] / vals[i - 1] > SPECIAL_RATIO and vals[i + 1] < vals[i]:
            had_special = True
            break

    return Streak(
        years=streak,
        non_decreasing=non_dec,
        latest_fy=int(fys[-1]),
        latest_dps=round(float(vals[-1]), 2),
        history=[{"fy": int(f), "dps": round(float(v), 2)} for f, v in zip(fys, vals)],
        had_special=had_special,
        complete_years=len(vals),
        # 取得できた全期間で増配していた場合、記録の始点は取得窓の外にある。
        # 花王の35期連続増配を15年分のデータで数えれだ14期にしかならず、
        # 「14期」と断定すると実態を大きく下回る値を出すことになる。
        capped=(streak > 0 and streak == len(vals) - 1),
    )


def label(s: Streak) -> str:
    """画面に出す短い文言。"""
    if s.complete_years < 2:
        return "履歴不足"
    if s.years >= 1:
        # 取得窓いっぱいまで増配が続いていたら、始点は窓の外。断定しない。
        return f"{s.years}期以上連続増配" if s.capped else f"{s.years}期連続増配"
    if s.non_decreasing >= 1:
        return f"{s.non_decreasing}期非減配"
    return "減配あり"


def load_history(csv_path) -> dict[str, pd.Series]:
    """fetch_dividend_history.py が書いた CSV を code -> Series に読み込む。

    index は権利落ち日。ファイルが無ければ空の辞書を返す（全銘柄が「判定なし」に
    なるだけで、ビルドは止めない）。
    """
    path = Path(csv_path)
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype={"code": str})
    if df.empty:
        return {}
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    out: dict[str, pd.Series] = {}
    for code, grp in df.groupby("code"):
        s = grp.set_index("ex_date")["amount"].sort_index()
        out[str(code)] = s
    return out


def attach(stocks: list[dict], fetch_dividends, now: pd.Timestamp | None = None) -> list[dict]:
    """各銘柄に streak 情報を足す。

    fetch_dividends(code) -> pd.Series（index=**権利落ち日**, value=1株あたり配当）
    を渡す。yfinance なら `yf.Ticker(f"{code}.T").dividends` がそのまま使える。
    支払日ベースの系列を渡すと集計が1期ずれるので注意。
    取得に失敗した銘柄は None を返せばよく、その場合は判定を付けない。
    """
    for st in stocks:
        try:
            div = fetch_dividends(st["code"])
        except Exception:
            div = None
        if div is None or len(div) == 0:
            st["streak"] = None
            continue
        s = compute_streak(div, now=now)
        st["streak"] = {**asdict(s), "label": label(s)}
    return stocks
