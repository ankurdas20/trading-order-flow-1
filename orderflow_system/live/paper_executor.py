"""
Simulates paper execution of a validated trade decision by walking forward
through subsequent bars until the stop or target is touched (using bar
high/low, not just close, so intrabar touches are respected).

In true live operation this module is replaced by real order placement
against IBKR's paper trading account — the interface (a trade plan in,
a fill/outcome out) stays the same, which is why it's cleanly separable.
"""
import duckdb
import pandas as pd
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db
from live.decision_engine import TradeDecision


def simulate_execution(con: duckdb.DuckDBPyConnection, symbol: str, entry_bar_ts,
                        decision: TradeDecision, max_bars_forward: int = 200) -> dict:
    future_bars = con.execute("""
        SELECT bar_ts, high, low, close FROM bars
        WHERE symbol = ? AND bar_ts > ?
        ORDER BY bar_ts LIMIT ?
    """, [symbol, entry_bar_ts, max_bars_forward]).fetchdf()

    if future_bars.empty:
        return {"outcome": "no_data", "exit_price": None, "exit_ts": None, "bars_held": 0}

    for i, row in future_bars.iterrows():
        if decision.trade == "long":
            hit_stop = row["low"] <= decision.stop_loss
            hit_target = row["high"] >= decision.take_profit
        else:  # short
            hit_stop = row["high"] >= decision.stop_loss
            hit_target = row["low"] <= decision.take_profit

        # Conservative assumption: if both could have hit within the same bar,
        # assume the stop hit first (worse-case, avoids inflating paper results).
        if hit_stop:
            return {"outcome": "stop", "exit_price": decision.stop_loss,
                    "exit_ts": row["bar_ts"], "bars_held": i + 1}
        if hit_target:
            return {"outcome": "target", "exit_price": decision.take_profit,
                    "exit_ts": row["bar_ts"], "bars_held": i + 1}

    # Neither hit within max_bars_forward -> mark to last available close
    last = future_bars.iloc[-1]
    return {"outcome": "timeout", "exit_price": last["close"],
            "exit_ts": last["bar_ts"], "bars_held": len(future_bars)}


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    fake_decision = TradeDecision(
        trade="short", entry=2402.1, stop_loss=2403.1, take_profit=2399.1,
        risk_reward=3.0, confidence=65, reasoning="test"
    )
    import datetime
    result = simulate_execution(con, "GC", datetime.datetime(2026, 7, 6, 17, 35, 0), fake_decision)
    print("Simulated execution result:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    con.close()
