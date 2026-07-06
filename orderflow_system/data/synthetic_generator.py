"""
Synthetic tick data generator — TESTING ONLY.

This exists so the entire pipeline (ingestion -> bars -> features -> decision
-> paper execution -> journal) can be proven to work end-to-end without
needing live IBKR/Databento credentials in this environment.

It deliberately injects a few "absorption" patterns (volume spike, minimal
price movement) near a simulated VAL/VAH level so you can verify the feature
engine actually detects what it's supposed to detect.

REPLACE THIS with data/ibkr_ingest.py or data/databento_ingest.py when you
run the system on your own machine with real market access. Same output
shape (rows into raw_ticks) so nothing downstream needs to change.
"""
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db


def generate_session_ticks(symbol: str, tick_size: float, start_price: float,
                            session_date: date, session_minutes: int = 390,
                            seed: int = 42, target_val: float | None = None,
                            target_vah: float | None = None) -> tuple[list[dict], float, float]:
    """Returns (ticks, val_price, vah_price). If target_val/target_vah are given
    (the prior session's actual VAL/VAH), today's planted absorption events are
    placed there instead of an arbitrary offset from today's start price — this
    is what lets the VAL/VAH-absorption-reversal edge actually fire on day 2+
    now that the feature engine correctly references the PRIOR session's levels."""
    rng = np.random.default_rng(seed)
    ticks = []
    price = start_price
    t = datetime(session_date.year, session_date.month, session_date.day, 13, 30, 0)

    # Rough value area boundaries used to place deliberate absorption events at
    # realistic spots. Use the prior session's real levels when available.
    val_price = target_val if target_val is not None else start_price - 20 * tick_size
    vah_price = target_vah if target_vah is not None else start_price + 20 * tick_size

    # Pre-plan a handful of absorption events near VAL/VAH
    absorption_minutes = sorted(rng.choice(range(30, session_minutes - 30), size=4, replace=False))

    minute = 0
    while minute < session_minutes:
        is_absorption_minute = minute in absorption_minutes
        n_ticks_this_minute = rng.integers(15, 40)

        if is_absorption_minute:
            # Force price near VAL or VAH, simulate heavy volume with minimal drift
            target = val_price if rng.random() < 0.5 else vah_price
            price = target
            drift_ticks_total = rng.integers(-2, 3)  # tiny net movement despite volume
            n_ticks_this_minute = rng.integers(60, 100)  # volume spike
        else:
            drift_ticks_total = rng.integers(-5, 6)

        drift_per_tick = (drift_ticks_total * tick_size) / max(n_ticks_this_minute, 1)

        for i in range(n_ticks_this_minute):
            price += drift_per_tick + rng.normal(0, tick_size * 0.3)
            price = round(price / tick_size) * tick_size
            size = int(rng.integers(1, 10)) if not is_absorption_minute else int(rng.integers(5, 25))
            # crude aggressor inference for synthetic data: random but volume-weighted
            aggressor = "buy" if rng.random() < 0.5 else "sell"
            ts = t + timedelta(minutes=minute, seconds=(i * 60 / max(n_ticks_this_minute, 1)))
            ticks.append({
                "symbol": symbol, "ts": ts, "price": price,
                "size": size, "aggressor": aggressor
            })
        minute += 1

    return ticks, val_price, vah_price


def business_days_ending(end_date: date, n_sessions: int) -> list[date]:
    """Last n_sessions weekdays up to and including end_date, oldest first."""
    days = []
    d = end_date
    while len(days) < n_sessions:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def load_synthetic_data(con: duckdb.DuckDBPyConnection, cfg: dict, n_sessions: int | None = None):
    """
    Generates n_sessions consecutive trading days of synthetic ticks per symbol.
    Each day's start price carries over (with a small random overnight gap) from
    the previous day's close, so multi-day features (prior-session VAL/VAH) are
    meaningful rather than arbitrary. Defaults to cfg['data']['synthetic_sessions']
    (falls back to 2 — the minimum needed for the "prior session" edge to fire at
    all, since day 1 has no prior session to reference).
    """
    if n_sessions is None:
        n_sessions = cfg["data"].get("synthetic_sessions", 2)

    end_date = date(2026, 7, 6)
    session_dates = business_days_ending(end_date, n_sessions)

    for sym_cfg in cfg["symbols"]:
        symbol = sym_cfg["name"]
        tick_size = sym_cfg["tick_size"]
        price = 20000.0 if symbol == "NQ" else 2400.0
        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        total = 0
        prev_val, prev_vah = None, None
        for i, session_date in enumerate(session_dates):
            seed = int(rng.integers(0, 2**31 - 1))
            ticks, val_price, vah_price = generate_session_ticks(
                symbol, tick_size, price, session_date, seed=seed,
                target_val=prev_val, target_vah=prev_vah
            )
            # Vectorized insert via a DataFrame — con.executemany() with Python
            # tuples is ~1000x slower in DuckDB (row-by-row) and doesn't scale
            # past a session or two once you're generating backtest data.
            df = pd.DataFrame(ticks)[["symbol", "ts", "price", "size", "aggressor"]]
            con.execute("INSERT INTO raw_ticks SELECT * FROM df")
            total += len(ticks)
            # carry the day's closing price and its val/vah forward so tomorrow's
            # planted absorption events reference today's actual levels
            price = ticks[-1]["price"] + rng.normal(0, tick_size * 5)
            prev_val, prev_vah = val_price, vah_price
        print(f"  Loaded {total} synthetic ticks for {symbol} across {n_sessions} session(s)")


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    con.execute("DELETE FROM raw_ticks")  # clean slate for repeatable testing
    print("Generating synthetic tick data...")
    load_synthetic_data(con, cfg)
    count = con.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0]
    print(f"Total ticks in raw_ticks: {count}")
    con.close()
