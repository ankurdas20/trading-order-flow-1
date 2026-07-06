"""
Computes session volume profile: Point of Control (POC), Value Area High (VAH),
Value Area Low (VAL) — the reference levels your V1 edge (VAL/VAH absorption
reversal) is built around.

Built directly from raw_ticks (price binned to tick size) rather than from
OHLC bars, since bars throw away the actual traded-price distribution within
each bar.
"""
import duckdb
import pandas as pd
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db


def compute_session_profile(con: duckdb.DuckDBPyConnection, symbol: str,
                             tick_size: float, session_date: str,
                             value_area_pct: float = 0.70) -> dict:
    df = con.execute("""
        SELECT price, size FROM raw_ticks
        WHERE symbol = ? AND CAST(ts AS DATE) = ?
    """, [symbol, session_date]).fetchdf()

    if df.empty:
        return {}

    df["price_bin"] = (df["price"] / tick_size).round() * tick_size
    profile = df.groupby("price_bin")["size"].sum().sort_index()

    poc_price = profile.idxmax()
    total_volume = profile.sum()
    target_volume = total_volume * value_area_pct

    # Expand outward from POC, adding whichever adjacent level has more volume,
    # until we've captured value_area_pct of total volume.
    prices = list(profile.index)
    poc_idx = prices.index(poc_price)
    included = {poc_idx}
    captured = profile.iloc[poc_idx]

    lo, hi = poc_idx, poc_idx
    while captured < target_volume and (lo > 0 or hi < len(prices) - 1):
        vol_below = profile.iloc[lo - 1] if lo > 0 else -1
        vol_above = profile.iloc[hi + 1] if hi < len(prices) - 1 else -1
        if vol_above >= vol_below:
            hi += 1
            captured += profile.iloc[hi]
            included.add(hi)
        else:
            lo -= 1
            captured += profile.iloc[lo]
            included.add(lo)

    val_price = prices[lo]
    vah_price = prices[hi]

    return {"poc": float(poc_price), "vah": float(vah_price), "val": float(val_price)}


def update_all_session_profiles(con: duckdb.DuckDBPyConnection, cfg: dict):
    dates = con.execute("SELECT DISTINCT CAST(ts AS DATE) AS d FROM raw_ticks").fetchall()
    con.execute("DELETE FROM session_profile")
    for sym_cfg in cfg["symbols"]:
        symbol = sym_cfg["name"]
        tick_size = sym_cfg["tick_size"]
        for (d,) in dates:
            profile = compute_session_profile(con, symbol, tick_size, str(d))
            if profile:
                con.execute("""
                    INSERT INTO session_profile (symbol, session_date, poc, vah, val)
                    VALUES (?, ?, ?, ?, ?)
                """, [symbol, d, profile["poc"], profile["vah"], profile["val"]])
                print(f"  {symbol} {d}: POC={profile['poc']:.2f}  VAH={profile['vah']:.2f}  VAL={profile['val']:.2f}")


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    print("Computing session volume profiles...")
    update_all_session_profiles(con, cfg)
    con.close()
