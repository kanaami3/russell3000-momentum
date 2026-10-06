"""前回の出力からAI生成物だけを引き継ぐ共通処理。

数値データを作るスクリプト（calc_value_rankings / build_ipo_page /
build_dividend_screener など）は、出力ファイルを毎回まるごと書き直す。
その後ろで動くAIスクリプトが失敗すると、前回ぶんのAI生成物まで一緒に
消えてしまい、サイトからセクションごと消える。これを防ぐための処理。

**古い内容は引き継がない。**
「昨日の解説が今日も載っている」は許容範囲だが、2週間前の相場解説が
今日の日付で出ているのは誤情報になる。日数で足切りする。

**空の値は引き継がない。**
前回が空文字や空リストなら引き継ぐ意味がないので、真と評価できる値だけ
を対象にする。
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

# 引き継ぎを許す古さの上限（日）。
DEFAULT_MAX_DAYS = 7

# 鮮度の判断に使うキー。上から順に見つかったものを使う。
_STAMP_KEYS = ("asof", "generated_at_jst", "generated_at", "target_date")


def _as_date(value):
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def carry_previous_ai(path, keys, asof=None, max_days=DEFAULT_MAX_DAYS, verbose=True):
    """既存ファイルから keys のうち中身のあるものだけを dict で返す。

    path    : 書き込み先（＝前回の出力）
    keys    : 引き継ぎたいキー名のリスト
    asof    : 今回のデータの基準日。省略時は本日。
    max_days: これより古い内容は引き継がない

    ファイルが無い・壊れている・古すぎる場合は空の dict を返す。
    呼び出し側は戻り値をそのまま payload.update() すればよい。
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        prev = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(prev, dict):
        return {}

    found = {k: prev[k] for k in keys if prev.get(k)}
    if not found:
        return {}

    prev_date = None
    for k in _STAMP_KEYS:
        prev_date = _as_date(prev.get(k))
        if prev_date:
            break
    if prev_date is None:
        # 日付が分からないものは引き継がない。古いかどうか判断できないため。
        return {}

    base = _as_date(asof) or _dt.date.today()
    age = (base - prev_date).days

    if age > max_days:
        if verbose:
            print(f"  前回のAI内容は {prev_date} 時点（{age}日前）のため引き継ぎません",
                  file=sys.stderr)
        return {}

    if verbose:
        names = "/".join(found.keys())
        print(f"  前回のAI内容を引き継ぎました（{prev_date} 時点 / {age}日前 / {names}）",
              file=sys.stderr)
    return found
