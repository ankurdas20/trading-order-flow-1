"""
Walk-forward backtest engine.

Generates many synthetic trading sessions, runs the full pipeline (bars ->
volume profile -> features -> strategy filter -> decision engine -> paper
execution) across all of them, then splits the resulting trade log into
chronological folds to check whether the edge's performance is consistent
over time or just noise from one lucky/unlucky stretch.

Important honesty note: this build's decision engine (mock or Gemini) has no
free parameters that get FIT to data — the strategy thresholds are fixed in
config.yaml. So "walk-forward" here isn't preventing parameter overfitting
(there's nothing to overfit yet); it's an out-of-time consistency check —
does the edge behave the same way across independent chunks of the past, or
does performance swing wildly fold to fold? That distinction matters: once
this graduates from mock/synthetic to real market data, this same mechanism
is what you'd point at real historical sessions, and a walk-forward split
becomes essential the moment you (or the AI) start tuning any thresholds
based on backtest results — at that point you MUST tune only on train folds
and judge only on test folds, or you're just overfitting with extra steps.

Usage:
    python3 -m backtest.engine --sessions 40 --folds 4
"""
import argparse
import sys
from pathlib import Path
from statistics import mean, pstdev

import duckdb
import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db
from data.synthetic_generator import load_synthetic_data
from data.aggregator import build_bars
from features.volume_profile import update_all_session_profiles
from features.engine import run_feature_engine
from strategies.val_absorption_reversal import find_candidates
from live.decision_engine import decide
from live.risk_engine import size_position
from live.paper_executor import simulate_execution


def run_full_pipeline(con: duckdb.DuckDBPyConnection, cfg: dict, n_sessions: int) -> pd.DataFrame:
    """Runs ingestion through paper execution once, over n_sessions of data,
    and returns one row per trade actually taken (no_trade rows excluded —
    this is a trade log, not a decision log; use main.py + the journal for
    a full decision log including no-trades)."""
    con.execute("DELETE FROM raw_ticks")
    load_synthetic_data(con, cfg, n_sessions=n_sessions)
    build_bars(con, cfg["data"]["bar_interval_seconds"])
    update_all_session_profiles(con, cfg)
    run_feature_engine(con, cfg)
    candidates = find_candidates(con)

    trades = []
    for _, row in candidates.iterrows():
        row_dict = row.to_dict()
        symbol = row_dict["symbol"]
        sym_cfg = next(s for s in cfg["symbols"] if s["name"] == symbol)

        decision = decide(row_dict, cfg)
        if decision.trade == "no_trade":
            continue
        sizing = size_position(decision, symbol, cfg)
        if sizing["contracts"] <= 0:
            continue

        outcome = simulate_execution(con, symbol, row_dict["bar_ts"], decision)
        direction_mult = 1 if decision.trade == "long" else -1
        pnl = round(
            direction_mult * (outcome["exit_price"] - decision.entry)
            * sizing["contracts"] * sym_cfg["point_value"], 2
        )

        trades.append({
            "session_date": pd.Timestamp(row_dict["bar_ts"]).date(),
            "symbol": symbol,
            "bar_ts": row_dict["bar_ts"],
            "direction": decision.trade,
            "confidence": decision.confidence,
            "risk_reward_planned": decision.risk_reward,
            "outcome": outcome["outcome"],
            "contracts": sizing["contracts"],
            "risk_dollars": sizing["risk_dollars"],
            "pnl_dollars": pnl,
        })

    return pd.DataFrame(trades)


def make_folds(trades: pd.DataFrame, n_folds: int) -> list[pd.DataFrame]:
    """Splits the trade log into n_folds contiguous chronological blocks by
    session date (not by trade count) so each fold represents a distinct
    stretch of calendar time, which is what "walk-forward" is checking for."""
    if trades.empty:
        return []
    session_dates = sorted(trades["session_date"].unique())
    if len(session_dates) < n_folds:
        n_folds = max(1, len(session_dates))

    date_folds = [list(chunk) for chunk in _chunk(session_dates, n_folds)]
    return [trades[trades["session_date"].isin(dates)].copy() for dates in date_folds]


def _chunk(seq, n):
    k, m = divmod(len(seq), n)
    start = 0
    for i in range(n):
        size = k + (1 if i < m else 0)
        yield seq[start:start + size]
        start += size


def fold_stats(fold: pd.DataFrame) -> dict:
    n = len(fold)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "expectancy": None, "total_pnl": 0.0}
    wins = int((fold["outcome"] == "target").sum())
    total_pnl = float(fold["pnl_dollars"].sum())
    return {
        "n_trades": n,
        "win_rate": round(wins / n * 100, 1),
        "expectancy": round(total_pnl / n, 2),
        "total_pnl": round(total_pnl, 2),
    }


def overfitting_verdict(per_fold: list[dict], min_trades_total: int = 30) -> tuple[str, str]:
    total_trades = sum(f["n_trades"] for f in per_fold)
    scored = [f for f in per_fold if f["n_trades"] > 0]

    if total_trades < min_trades_total:
        return "INSUFFICIENT_DATA", (
            f"Only {total_trades} trades total across all folds (need {min_trades_total}+ "
            f"to say anything statistically meaningful). Treat any result below as noise."
        )

    expectancies = [f["expectancy"] for f in scored]
    signs = [1 if e > 0 else (-1 if e < 0 else 0) for e in expectancies]
    positive_folds = sum(1 for s in signs if s > 0)
    negative_folds = sum(1 for s in signs if s < 0)

    if positive_folds == len(scored):
        return "ROBUST", "Every fold had positive expectancy. Consistent across all time chunks tested."
    if negative_folds == len(scored):
        return "DEAD", "Every fold had negative expectancy. This edge (as implemented) loses money consistently — kill it, don't tune it."
    if positive_folds >= len(scored) - 1:
        return "MODERATE", "Most folds positive, one weak/negative fold. Plausible edge, but watch that one fold's regime before trusting this fully."
    if abs(positive_folds - negative_folds) <= 1:
        return "WEAK", "Roughly even split of winning and losing folds. This looks like noise, not a stable edge, at this sample size."
    return "INCONSISTENT", "Performance swings substantially fold to fold — likely regime-dependent rather than a stable edge. Investigate per-fold regime mix before proceeding."


def run_backtest(n_sessions: int, n_folds: int, cfg_path: str | None = None) -> dict:
    cfg = load_config(cfg_path)
    con = init_db(cfg["database"]["path"])

    print("=" * 60)
    print(f"WALK-FORWARD BACKTEST — {n_sessions} synthetic sessions, {n_folds} folds")
    print("=" * 60)
    if cfg["data"]["source"] == "synthetic":
        print("NOTE: running on synthetic data with PLANTED absorption setups at the prior")
        print("day's VAL/VAH — a good verdict here proves the walk-forward MECHANISM works,")
        print("not that a real edge exists. Point data.source at Databento/IBKR before trusting")
        print("any verdict as evidence about real markets.")

    trades = run_full_pipeline(con, cfg, n_sessions)
    con.close()

    print(f"\nTotal trades taken across all sessions: {len(trades)}")
    if trades.empty:
        print("No trades were taken across the entire backtest window — nothing to fold-split.")
        return {"trades": trades, "folds": [], "verdict": ("INSUFFICIENT_DATA", "No trades taken.")}

    folds = make_folds(trades, n_folds)
    per_fold = [fold_stats(f) for f in folds]

    print("\nPer-fold results (chronological, oldest first):")
    for i, (fold, stats) in enumerate(zip(folds, per_fold), start=1):
        date_range = (
            f"{fold['session_date'].min()} -> {fold['session_date'].max()}" if not fold.empty else "n/a"
        )
        print(f"  Fold {i} [{date_range}]: trades={stats['n_trades']}  "
              f"win_rate={stats['win_rate']}%  expectancy=${stats['expectancy']}  "
              f"total_pnl=${stats['total_pnl']}")

    verdict, explanation = overfitting_verdict(per_fold)
    print(f"\nVERDICT: {verdict}")
    print(f"  {explanation}")

    overall = fold_stats(trades)
    print(f"\nOverall (all folds combined): trades={overall['n_trades']}  "
          f"win_rate={overall['win_rate']}%  expectancy=${overall['expectancy']}  "
          f"total_pnl=${overall['total_pnl']}")

    return {"trades": trades, "folds": per_fold, "verdict": (verdict, explanation), "overall": overall}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-forward backtest for the order flow system")
    parser.add_argument("--sessions", type=int, default=40, help="number of synthetic trading days to generate")
    parser.add_argument("--folds", type=int, default=4, help="number of chronological folds to split into")
    args = parser.parse_args()
    run_backtest(args.sessions, args.folds)
