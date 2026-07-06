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
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db


def generate_session_ticks(symbol: str, tick_size: float, start_price: float,
                            session_minutes: int = 390, seed: int = 42) -> list[dict]:
    rng = np.random.default_rng(seed)
    ticks = []
    price = start_price
    t = datetime(2026, 7, 6, 13, 30, 0)

    # Rough value area boundaries for this synthetic session, used to place
    # deliberate absorption events at realistic spots.
    val_price = start_price - 20 * tick_size
    vah_price = start_price + 20 * tick_size

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

    return ticks


def load_synthetic_data(con: duckdb.DuckDBPyConnection, cfg: dict):
    for sym_cfg in cfg["symbols"]:
        symbol = sym_cfg["name"]
        tick_size = sym_cfg["tick_size"]
        start_price = 20000.0 if symbol == "NQ" else 2400.0
        ticks = generate_session_ticks(symbol, tick_size, start_price)
        con.executemany(
            "INSERT INTO raw_ticks (symbol, ts, price, size, aggressor) VALUES (?, ?, ?, ?, ?)",
            [(t["symbol"], t["ts"], t["price"], t["size"], t["aggressor"]) for t in ticks]
        )
        print(f"  Loaded {len(ticks)} synthetic ticks for {symbol}")


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    con.execute("DELETE FROM raw_ticks")  # clean slate for repeatable testing
    print("Generating synthetic tick data...")
    load_synthetic_data(con, cfg)
    count = con.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0]
    print(f"Total ticks in raw_ticks: {count}")
    con.close()
