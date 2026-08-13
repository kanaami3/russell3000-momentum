#!/usr/bin/env python3
"""連続増配を4軸目として judge に組み込む。

judge は現在 dividend / earnings / value の3軸で、overall はその平均。
ここに growth（増配）を足して4軸平均にする。

設計上の判断が2つある。

**据え置きを×にしない。**
日本企業は配当を据え置く文化が強く、増配期数だけで測ると優良な安定配当銘柄が
軒並み低評価になる。連続増配（years）を主軸にしつつ、連続非減配
（non_decreasing）でも一定の評価を与える。

**履歴不足を×にしない。**
2023年上場の会社に「減配あり」と同じ×を付けるのは誤り。判定不能として None を
返し、**その銘柄の overall は残り3軸の平均で計算する**。0点として平均に混ぜると、
新規上場というだけで総合順位が不当に下がる。

閾値の較正: 日経連続増配株指数（10年以上連続増配）の対象は約70社。
◎を10期以上とすると、配当データのある銘柄の1割弱が該当する想定。
"""

from __future__ import annotations

import os

# ◎ に必要な連続増配期数
EXCELLENT_YEARS = int(os.getenv("DIV_GROWTH_EXCELLENT", "10"))
# ○ に必要な連続増配期数
GOOD_YEARS = int(os.getenv("DIV_GROWTH_GOOD", "5"))
# 据え置き中心でも評価する連続非減配期数
STABLE_YEARS = int(os.getenv("DIV_GROWTH_STABLE", "10"))
# 判定に最低限必要な確定期数
MIN_HISTORY = int(os.getenv("DIV_GROWTH_MIN_HISTORY", "3"))

AXES = ("dividend", "earnings", "value", "growth")


def growth_score(streak: dict | None) -> int | None:
    """連続増配を 1(×) 〜 4(◎) に落とす。判定不能なら None。"""
    if not streak:
        return None
    if streak.get("complete_years", 0) < MIN_HISTORY:
        return None                     # 履歴不足。×ではない。

    years = streak.get("years", 0)
    non_dec = streak.get("non_decreasing", 0)

    if years >= EXCELLENT_YEARS:
        return 4
    if years >= GOOD_YEARS and non_dec >= STABLE_YEARS:
        return 4                        # 増配基調かつ長期に減らしていない
    if years >= GOOD_YEARS or non_dec >= STABLE_YEARS:
        return 3
    if years >= 1 or non_dec >= GOOD_YEARS:
        return 2
    return 1                            # 直近で減配している


def overall(judge: dict) -> int:
    """利用可能な軸だけで平均する。

    None の軸を0点として混ぜると、データが無いことが減点になってしまう。
    """
    vals = [judge[a] for a in AXES if judge.get(a) is not None]
    if not vals:
        return 1
    return round(sum(vals) / len(vals))


def apply(stocks: list[dict]) -> list[dict]:
    """各銘柄の judge に growth を足し、overall を4軸平均で引き直す。

    dividend_streak.attach() を先に通しておくこと。
    """
    for st in stocks:
        j = st.setdefault("judge", {})
        j["growth"] = growth_score(st.get("streak"))
        j["overall"] = overall(j)
    return stocks


CRITERIA_GROWTH = (
    f"増配: 連続増配{EXCELLENT_YEARS}期以上=◎ / "
    f"{GOOD_YEARS}期以上または連続非減配{STABLE_YEARS}期以上=○ / "
    f"増配実績あり=△ / 直近減配=× / 履歴{MIN_HISTORY}期未満は判定なし"
)
