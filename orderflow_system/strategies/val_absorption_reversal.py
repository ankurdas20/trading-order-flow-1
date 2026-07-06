"""
V1 Strategy: VAL/VAH Absorption Reversal

This module does NOT decide whether to trade. It only filters the feature
stream down to candidate setups that match the edge hypothesis structurally.
The actual trade decision (entry/stop/target/reasoning) happens in the
decision engine, which only ever sees candidates that pass this filter.

Edge hypothesis (from config.yaml):
  Price is near session VAL or VAH (within proximity_ticks) AND an absorption
  event fires on the side consistent with a reversal at that level:
    - near VAL + buy_absorption  -> potential long (sellers being absorbed at support)
    - near VAH + sell_absorption -> potential short (buyers being absorbed at resistance)
"""
import duckdb
import pandas as pd
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db

PROXIMITY_TICKS = 8  # how close to VAL/VAH price must be to count as "at the level"


def find_candidates(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute("""
        SELECT f.*, b.open, b.high, b.low, b.close, b.volume, b.delta
        FROM features f
        JOIN bars b ON f.symbol = b.symbol AND f.bar_ts = b.bar_ts
        WHERE f.absorption_flag = TRUE
        ORDER BY f.bar_ts
    """).fetchdf()

    if df.empty:
        return df

    near_val = df["distance_to_val"].abs() <= PROXIMITY_TICKS
    near_vah = df["distance_to_vah"].abs() <= PROXIMITY_TICKS

    long_candidate = near_val & (df["absorption_side"] == "buy_absorption")
    short_candidate = near_vah & (df["absorption_side"] == "sell_absorption")

    df["candidate_direction"] = None
    df.loc[long_candidate, "candidate_direction"] = "long"
    df.loc[short_candidate, "candidate_direction"] = "short"

    candidates = df[df["candidate_direction"].notna()].copy()
    return candidates


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    candidates = find_candidates(con)
    print(f"Found {len(candidates)} candidate setups (structural filter only, not trade decisions)\n")
    if not candidates.empty:
        print(candidates[[
            "symbol", "bar_ts", "close", "candidate_direction",
            "distance_to_val", "distance_to_vah", "regime", "rel_volume", "cvd_slope"
        ]].to_string(index=False))
    con.close()
