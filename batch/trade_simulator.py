"""Simulated paper-trading account that follows the AI's daily picks.

Starting capital: ¥10,000,000 (configurable via START_CAPITAL).
Trading rules (applied to today's actual OHLCV):
  - Position size: 1/7 of starting capital per pick (round down to 100株 lot)
  - Buy fill: at session open if entry range covers it, else at entry range edge
    nearest to the day's path; skipped if today's range doesn't overlap entry.
  - 0.1% slippage applied to all fills.
  - Exit logic per pick.hold_horizon:
      "日中決済":
        - stop hit (low <= stop)            → exit at stop
        - target hit (high >= target_today) → exit at target_today
        - else                              → exit at close
      "翌日持越し可":
        - stop hit today                    → exit at stop
        - target_today hit today            → exit at target_today
        - else                              → carry overnight; close at next
                                              session's open OR earlier if
                                              stop / target_next triggers.
  - No commission; cash account; no leverage.

Inputs:
  - web/data/morning_brief_jp.json (today's AI picks)
  - data/prices_jp.csv             (OHLCV including today)
  - web/data/portfolio_jp.json     (state — created if missing)

Output: updated web/data/portfolio_jp.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIEF_PATH = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"
PRICES_PATH = REPO_ROOT / "data" / "prices_jp.csv"
PORTFOLIO_PATH = REPO_ROOT / "web" / "data" / "portfolio_jp.json"

START_CAPITAL = 10_000_000          # ¥10M virtual seed
N_TARGET_POSITIONS = 7              # AI picks ~7 per day; size each at 1/7
LOT_SIZE = 100                      # JP standard lot
SLIPPAGE_PCT = 0.001                # 0.1% per leg

DISCLAIMER = (
    "本機能はAIピックの仮想シミュレーションです。実際の取引・運用ではありません。"
    "本結果は将来の成果を保証するものではなく、投資勧誘・助言ではありません。"
)


# ---------------------------------------------------------------------------
# Parsing AI free-text entry/target/risk fields
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{2,7})")
_RANGE_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d{2,7})\s*[~〜～\-－]\s*(\d{1,3}(?:,\d{3})+|\d{2,7})"
)


def _to_num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_first_price(text: str) -> float | None:
    if not text:
        return None
    m = _NUM_RE.search(text)
    return _to_num(m.group(1)) if m else None


def parse_entry_range(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m = _RANGE_RE.search(text)
    if m:
        a, b = _to_num(m.group(1)), _to_num(m.group(2))
        return min(a, b), max(a, b)
    p = parse_first_price(text)
    return p, p


def parse_targets(text: str) -> tuple[float | None, float | None]:
    """Find '本日' and '翌日' target prices. Falls back to first / second price."""
    if not text:
        return None, None
    today_m = re.search(r"本日[^\d]{0,8}(\d{1,3}(?:,\d{3})+|\d{2,7})", text)
    next_m = re.search(r"翌日[^\d]{0,8}(\d{1,3}(?:,\d{3})+|\d{2,7})", text)
    today = _to_num(today_m.group(1)) if today_m else None
    nxt = _to_num(next_m.group(1)) if next_m else None
    if today is None and nxt is None:
        prices = _NUM_RE.findall(text)
        if prices:
            today = _to_num(prices[0])
            if len(prices) >= 2:
                nxt = _to_num(prices[1])
    return today, nxt


def _num_field(pick: dict, key: str) -> float | None:
    """Read a numeric field from the AI pick, tolerating strings like '5,100円'."""
    v = pick.get(key)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v else None
    try:
        return _to_num(str(v).replace("円", "").strip()) or None
    except (ValueError, AttributeError):
        return None


def resolve_levels(pick: dict, ref_price: float) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Resolve (entry_low, entry_high, stop, target_today, target_next).

    Prefers the AI's explicit numeric fields (entry_low/entry_high/stop/
    target_today/target_next); falls back to parsing the prose entry/target/
    risk text only when a numeric field is missing. Any level more than 60%
    away from ref_price (the day's open) is treated as a parse error and
    dropped — this rejects bogus values like an RSI (87) or a clock time (30)
    that the old text parser used to grab as a "price".
    """
    def sane(x):
        if x is None:
            return None
        if ref_price > 0 and (x < ref_price * 0.4 or x > ref_price * 1.6):
            return None
        return x

    entry_low = sane(_num_field(pick, "entry_low"))
    entry_high = sane(_num_field(pick, "entry_high"))
    stop = sane(_num_field(pick, "stop"))
    target_today = sane(_num_field(pick, "target_today"))
    target_next = sane(_num_field(pick, "target_next"))

    # Text fallbacks for anything still missing
    if entry_low is None or entry_high is None:
        el, eh = parse_entry_range(pick.get("entry", ""))
        entry_low = entry_low or sane(el)
        entry_high = entry_high or sane(eh)
    if target_today is None or target_next is None:
        tt, tn = parse_targets(pick.get("target", ""))
        target_today = target_today or sane(tt)
        target_next = target_next or sane(tn)
    if stop is None:
        # Risk text is the least reliable source; only trust it if it lands
        # in a sane band below entry.
        stop = sane(parse_first_price(pick.get("risk", "")))

    # Final coherence: entry_low ≤ entry_high
    if entry_low and entry_high and entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    if entry_low and entry_high is None:
        entry_high = entry_low
    return entry_low, entry_high, stop, target_today, target_next


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

def init_portfolio(today: str) -> dict:
    return {
        "disclaimer": DISCLAIMER,
        "start_date": today,
        "start_capital": START_CAPITAL,
        "cash": START_CAPITAL,
        "equity": START_CAPITAL,
        "total_pnl": 0,
        "total_pnl_pct": 0.0,
        "open_positions": [],
        "trade_history": [],
        "daily_equity": [{"date": today, "equity": START_CAPITAL, "daily_pnl": 0, "daily_pnl_pct": 0.0}],
        "stats": _empty_stats(),
        "last_simulated_date": None,
    }


def _empty_stats() -> dict:
    return {
        "trade_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "win_rate": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
        "best_trade_pct": 0.0,
        "worst_trade_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "running_days": 0,
    }


def load_or_init_portfolio(today: str) -> dict:
    if PORTFOLIO_PATH.exists():
        try:
            p = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
            # Backfill missing fields gracefully
            p.setdefault("disclaimer", DISCLAIMER)
            p.setdefault("trade_history", [])
            p.setdefault("open_positions", [])
            p.setdefault("daily_equity", [])
            p.setdefault("stats", _empty_stats())
            return p
        except Exception as e:
            print(f"WARN: failed to load portfolio ({e}); reinitializing.", file=sys.stderr)
    return init_portfolio(today)


def save_portfolio(p: dict) -> None:
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_PATH.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def get_ohlc(prices: pd.DataFrame, ticker: str, date: str) -> dict | None:
    row = prices[(prices["ticker"] == ticker) & (prices["date"] == date)]
    if row.empty:
        return None
    r = row.iloc[0]
    return {
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
        "volume": float(r["volume"]),
    }


def latest_ohlc(prices: pd.DataFrame, ticker: str) -> dict | None:
    sub = prices[prices["ticker"] == ticker].sort_values("date")
    if sub.empty:
        return None
    r = sub.iloc[-1]
    return {
        "date": str(r["date"]),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
    }


def _record_trade(p: dict, *, ticker: str, name: str, hold_type: str, ai_pick_type: str,
                  shares: int, entry_date: str, entry_price: float,
                  exit_date: str, exit_price: float, outcome: str) -> None:
    cost = entry_price * shares
    proceeds = exit_price * shares
    pnl = proceeds - cost
    pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
    p["trade_history"].append({
        "entry_date": entry_date,
        "exit_date": exit_date,
        "ticker": ticker,
        "name": name,
        "hold_type": hold_type,           # "day_trade" | "overnight_hold"
        "ai_pick_type": ai_pick_type,
        "shares": shares,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "pnl": int(round(pnl)),
        "pnl_pct": round(pnl_pct, 2),
        "outcome": outcome,
    })


def close_open_positions(p: dict, prices: pd.DataFrame, today: str) -> None:
    """Apply today's OHLCV to any positions held overnight from prior days."""
    keep: list[dict] = []
    for pos in p["open_positions"]:
        ticker = pos["ticker"]
        ohlc = get_ohlc(prices, ticker, today)
        if ohlc is None:
            # No data for today (delisted / suspended) — hold
            keep.append(pos)
            continue
        stop = pos.get("stop_price")
        target_next = pos.get("target_next") or pos.get("target_today")

        exit_price = None
        outcome = None
        # Check gap at open first
        if stop is not None and ohlc["open"] <= stop:
            exit_price = ohlc["open"] * (1 - SLIPPAGE_PCT)
            outcome = "gap_stop"
        elif target_next is not None and ohlc["open"] >= target_next:
            exit_price = ohlc["open"] * (1 - SLIPPAGE_PCT)
            outcome = "gap_target"
        elif stop is not None and ohlc["low"] <= stop:
            exit_price = stop * (1 - SLIPPAGE_PCT)
            outcome = "stop_hit"
        elif target_next is not None and ohlc["high"] >= target_next:
            exit_price = target_next * (1 - SLIPPAGE_PCT)
            outcome = "target_hit"
        else:
            # No trigger; planned exit at NEXT trading day's open per spec.
            # Since we're already on the next trading day, exit at today's open.
            exit_price = ohlc["open"] * (1 - SLIPPAGE_PCT)
            outcome = "next_open_close"

        proceeds = exit_price * pos["shares"]
        p["cash"] += proceeds
        _record_trade(
            p,
            ticker=ticker, name=pos["name"],
            hold_type=pos.get("hold_type", "overnight_hold"),
            ai_pick_type=pos.get("ai_pick_type", ""),
            shares=pos["shares"],
            entry_date=pos["entry_date"], entry_price=pos["entry_price"],
            exit_date=today, exit_price=exit_price,
            outcome=outcome,
        )
    p["open_positions"] = keep


def open_today_positions(p: dict, picks: list[dict], prices: pd.DataFrame, today: str) -> None:
    """Try to execute each AI pick against today's OHLC.

    Returns True if any new position opened, even if it was closed same-day.
    """
    if not picks:
        return

    # Position size = 1/N of starting capital so allocation stays consistent
    target_position_value = p["start_capital"] / N_TARGET_POSITIONS

    for pick in picks:
        ticker = pick.get("ticker")
        if not ticker:
            continue
        # Skip duplicates currently held
        if any(pos["ticker"] == ticker for pos in p["open_positions"]):
            continue

        ohlc = get_ohlc(prices, ticker, today)
        if ohlc is None:
            continue

        # Prefer the AI's explicit numeric fields; fall back to prose parsing.
        # ref_price = today's open, used to reject bogus levels (RSI, clock
        # times, percentages) that don't make sense as a share price.
        entry_low, entry_high, stop, target_today, target_next = resolve_levels(
            pick, ohlc["open"]
        )
        if not entry_low:
            continue
        if not stop:
            stop = entry_low * 0.97  # synthetic 3% stop when none given
        # Sanity: stop must be below entry (long-only sim)
        if stop >= entry_low:
            stop = entry_low * 0.97  # fall back to a synthetic 3% stop

        # Did today's range overlap the entry zone?
        if ohlc["high"] < entry_low or ohlc["low"] > entry_high:
            continue  # no fill today

        # Fill price selection
        if entry_low <= ohlc["open"] <= entry_high:
            fill_price = ohlc["open"]
        elif ohlc["open"] > entry_high:
            # Opened above entry; assume filled if pulled back into range
            fill_price = entry_high
        else:
            # Opened below entry; assume filled if range pushed up to it
            fill_price = entry_low
        fill_price *= (1 + SLIPPAGE_PCT)

        shares = int(target_position_value / fill_price / LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            continue
        cost = shares * fill_price
        if cost > p["cash"]:
            # Reduce to what we can afford
            shares = int(p["cash"] / fill_price / LOT_SIZE) * LOT_SIZE
            if shares <= 0:
                continue
            cost = shares * fill_price

        p["cash"] -= cost

        hold_horizon = pick.get("hold_horizon", "翌日持越し可")
        hold_type = "day_trade" if hold_horizon == "日中決済" else "overnight_hold"

        # Determine intraday outcome
        same_day_exit = None
        outcome = None
        if hold_type == "day_trade":
            # Day trade: must close today
            if ohlc["low"] <= stop:
                same_day_exit = stop * (1 - SLIPPAGE_PCT); outcome = "stop_hit"
            elif target_today and ohlc["high"] >= target_today:
                same_day_exit = target_today * (1 - SLIPPAGE_PCT); outcome = "target_hit"
            else:
                same_day_exit = ohlc["close"] * (1 - SLIPPAGE_PCT); outcome = "eod_close"
        else:
            # Overnight hold: only force-close if stop/today-target triggered today
            if ohlc["low"] <= stop:
                same_day_exit = stop * (1 - SLIPPAGE_PCT); outcome = "stop_hit_same_day"
            elif target_today and ohlc["high"] >= target_today:
                same_day_exit = target_today * (1 - SLIPPAGE_PCT); outcome = "target_today_hit"

        if same_day_exit is not None:
            p["cash"] += same_day_exit * shares
            _record_trade(
                p,
                ticker=ticker, name=pick.get("name", ticker),
                hold_type=hold_type,
                ai_pick_type=pick.get("type", ""),
                shares=shares,
                entry_date=today, entry_price=fill_price,
                exit_date=today, exit_price=same_day_exit,
                outcome=outcome,
            )
        else:
            # Carry overnight
            p["open_positions"].append({
                "ticker": ticker,
                "name": pick.get("name", ticker),
                "shares": shares,
                "entry_date": today,
                "entry_price": round(fill_price, 2),
                "stop_price": round(stop, 2),
                "target_today": round(target_today, 2) if target_today else None,
                "target_next": round(target_next, 2) if target_next else None,
                "hold_type": "overnight_hold",
                "ai_pick_type": pick.get("type", ""),
            })


def mark_to_market(p: dict, prices: pd.DataFrame) -> None:
    """Update open_positions' current price and unrealized P&L using latest close."""
    for pos in p["open_positions"]:
        info = latest_ohlc(prices, pos["ticker"])
        if info is None:
            pos["current_price"] = pos["entry_price"]
            pos["unrealized_pnl"] = 0
            pos["unrealized_pnl_pct"] = 0.0
            continue
        cp = info["close"]
        unreal = (cp - pos["entry_price"]) * pos["shares"]
        unreal_pct = (cp / pos["entry_price"] - 1) * 100
        pos["current_price"] = round(cp, 2)
        pos["unrealized_pnl"] = int(round(unreal))
        pos["unrealized_pnl_pct"] = round(unreal_pct, 2)


def compute_equity(p: dict) -> float:
    open_value = sum(pos.get("current_price", pos["entry_price"]) * pos["shares"]
                     for pos in p["open_positions"])
    return p["cash"] + open_value


def update_daily_equity(p: dict, today: str) -> None:
    equity = compute_equity(p)
    prev_equity = p["daily_equity"][-1]["equity"] if p["daily_equity"] else p["start_capital"]
    daily_pnl = equity - prev_equity
    daily_pnl_pct = (daily_pnl / prev_equity * 100) if prev_equity else 0
    # Replace or append today's record
    if p["daily_equity"] and p["daily_equity"][-1]["date"] == today:
        p["daily_equity"][-1] = {
            "date": today, "equity": int(round(equity)),
            "daily_pnl": int(round(daily_pnl)), "daily_pnl_pct": round(daily_pnl_pct, 2),
        }
    else:
        p["daily_equity"].append({
            "date": today, "equity": int(round(equity)),
            "daily_pnl": int(round(daily_pnl)), "daily_pnl_pct": round(daily_pnl_pct, 2),
        })
    p["equity"] = int(round(equity))
    p["total_pnl"] = int(round(equity - p["start_capital"]))
    p["total_pnl_pct"] = round((equity / p["start_capital"] - 1) * 100, 2)


def update_stats(p: dict) -> None:
    history = p["trade_history"]
    n = len(history)
    if n == 0:
        p["stats"] = _empty_stats()
        p["stats"]["running_days"] = len(p["daily_equity"])
        return
    wins = [t for t in history if t["pnl"] > 0]
    losses = [t for t in history if t["pnl"] < 0]
    pcts = [t["pnl_pct"] for t in history]
    # Max drawdown from daily equity curve
    peak = -1e18
    dd_pct = 0.0
    for d in p["daily_equity"]:
        peak = max(peak, d["equity"])
        if peak > 0:
            dd = (d["equity"] - peak) / peak * 100
            dd_pct = min(dd_pct, dd)
    p["stats"] = {
        "trade_count": n,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0.0,
        "best_trade_pct": round(max(pcts), 2),
        "worst_trade_pct": round(min(pcts), 2),
        "max_drawdown_pct": round(dd_pct, 2),
        "running_days": len(p["daily_equity"]),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not PRICES_PATH.exists():
        print(f"ERROR: missing {PRICES_PATH}", file=sys.stderr)
        return 1
    prices = pd.read_csv(PRICES_PATH, dtype={"ticker": str, "date": str})
    if "open" not in prices.columns:
        print("ERROR: prices_jp.csv missing OHLCV columns — re-run fetch_prices.py jp first", file=sys.stderr)
        return 1
    today = str(prices["date"].max())
    print(f"[SIM] Latest trading day in prices: {today}", file=sys.stderr)

    portfolio = load_or_init_portfolio(today)
    if portfolio.get("last_simulated_date") == today:
        print(f"[SIM] Already simulated {today}; refreshing mark-to-market only.", file=sys.stderr)
        mark_to_market(portfolio, prices)
        update_daily_equity(portfolio, today)
        update_stats(portfolio)
        save_portfolio(portfolio)
        return 0

    # 1. Close existing overnight positions using today's OHLC
    if portfolio["open_positions"]:
        print(f"[SIM] Closing {len(portfolio['open_positions'])} overnight position(s)", file=sys.stderr)
        close_open_positions(portfolio, prices, today)

    # 2. Open new positions from today's morning brief (if picks intended for today)
    if BRIEF_PATH.exists():
        brief = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
        ai_picks = brief.get("ai_picks") or []
        # Only act on picks whose intended session is today (asof < today)
        brief_asof = str(brief.get("asof", ""))
        if brief_asof and brief_asof < today and ai_picks:
            print(f"[SIM] Trying {len(ai_picks)} AI picks (brief asof={brief_asof}, session={today})", file=sys.stderr)
            open_today_positions(portfolio, ai_picks, prices, today)
        else:
            print(f"[SIM] Skipping picks: brief asof={brief_asof}, today={today}", file=sys.stderr)
    else:
        print(f"[SIM] No morning brief yet — skipping new entries", file=sys.stderr)

    # 3. Mark-to-market + daily snapshot + stats
    mark_to_market(portfolio, prices)
    update_daily_equity(portfolio, today)
    update_stats(portfolio)
    portfolio["last_simulated_date"] = today

    save_portfolio(portfolio)
    print(
        f"[SIM] Equity ¥{portfolio['equity']:,} ({portfolio['total_pnl_pct']:+.2f}%) | "
        f"open={len(portfolio['open_positions'])} | trades={portfolio['stats']['trade_count']} "
        f"(win {portfolio['stats']['win_rate']}%)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
