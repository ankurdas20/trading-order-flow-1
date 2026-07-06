"""
Core feature engine. Computes, per bar, within its own trading session:
  - CVD (cumulative volume delta, session-anchored) and its slope
  - absorption flag + side (volume spike with minimal price movement)
  - distance to the PRIOR session's POC / VAH / VAL (in ticks)
  - ADX + regime classification (trending vs ranging)
  - relative volume vs rolling average

Distance-to-level features deliberately reference the PRIOR completed
session's profile, never the current (still-forming) session's profile —
using today's own profile as a feature for an intraday bar today is
look-ahead bias, since the real POC/VAH/VAL for today isn't known until
the session closes. This also matches the edge hypothesis in config.yaml
("price reaches PRIOR session's VAL/VAH").

This is the module your conviction decision (rule-based or AI) should be
built on top of. Never feed raw ticks or raw bars directly to a decision
step — always go through here first.
"""
import duckdb
import pandas as pd
import numpy as np
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db


def compute_adx(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return adx


def _compute_session_features(day_bars: pd.DataFrame, prior_profile: dict | None,
                               tick_size: float, cfg: dict) -> pd.DataFrame:
    """Computes features for a single session's bars, in isolation, so nothing
    (CVD, rolling averages, ADX) leaks across session boundaries."""
    bars = day_bars.sort_values("bar_ts").reset_index(drop=True)
    feat_cfg = cfg["features"]

    # --- CVD: cumulative delta, anchored to this session's start ---
    bars["cvd"] = bars["delta"].cumsum()
    lookback = feat_cfg["cvd_lookback_bars"]
    bars["cvd_slope"] = bars["cvd"].diff(lookback)

    # --- Relative volume ---
    bars["rel_volume"] = bars["volume"] / bars["volume"].rolling(lookback, min_periods=5).mean()

    # --- Absorption: volume spike + minimal price movement ---
    price_move_ticks = (bars["close"] - bars["open"]).abs() / tick_size
    vol_avg = bars["volume"].rolling(lookback, min_periods=5).mean()
    is_vol_spike = bars["volume"] >= (feat_cfg["absorption_volume_multiple"] * vol_avg)
    is_low_move = price_move_ticks <= feat_cfg["absorption_max_price_move_ticks"]
    bars["absorption_flag"] = (is_vol_spike & is_low_move).fillna(False)

    def absorption_side(row):
        if not row["absorption_flag"]:
            return None
        # heavy net selling (negative delta) but price held up => buyers absorbing => support
        if row["delta"] < 0:
            return "buy_absorption"
        elif row["delta"] > 0:
            return "sell_absorption"
        return None

    bars["absorption_side"] = bars.apply(absorption_side, axis=1)

    # --- Distance to PRIOR session's levels (in ticks) ---
    if prior_profile:
        bars["distance_to_poc"] = (bars["close"] - prior_profile["poc"]) / tick_size
        bars["distance_to_val"] = (bars["close"] - prior_profile["val"]) / tick_size
        bars["distance_to_vah"] = (bars["close"] - prior_profile["vah"]) / tick_size
    else:
        bars["distance_to_poc"] = np.nan
        bars["distance_to_val"] = np.nan
        bars["distance_to_vah"] = np.nan

    # --- Regime via ADX (computed within this session only) ---
    bars["adx"] = compute_adx(bars, feat_cfg["regime_adx_period"])
    bars["regime"] = np.where(
        bars["adx"] >= feat_cfg["regime_adx_trend_threshold"], "trending", "ranging"
    )
    bars.loc[bars["adx"].isna(), "regime"] = None

    return bars


def compute_features_for_symbol(bars: pd.DataFrame, profiles_by_date: dict,
                                 tick_size: float, cfg: dict) -> pd.DataFrame:
    """
    profiles_by_date: {session_date (date): {"poc":.., "vah":.., "val":..}}
    for every session available for this symbol. Each bar looks up the most
    recent profile dated strictly BEFORE its own session date.
    """
    bars = bars.sort_values("bar_ts").reset_index(drop=True)
    session_dates = bars["bar_ts"].dt.date
    profile_dates_sorted = sorted(profiles_by_date.keys())

    out_frames = []
    for session_date, day_bars in bars.groupby(session_dates):
        prior_dates = [d for d in profile_dates_sorted if d < session_date]
        prior_profile = profiles_by_date[prior_dates[-1]] if prior_dates else None
        out_frames.append(_compute_session_features(day_bars, prior_profile, tick_size, cfg))

    return pd.concat(out_frames, ignore_index=True) if out_frames else bars.iloc[0:0]


def run_feature_engine(con: duckdb.DuckDBPyConnection, cfg: dict):
    con.execute("DELETE FROM features")
    for sym_cfg in cfg["symbols"]:
        symbol = sym_cfg["name"]
        tick_size = sym_cfg["tick_size"]
        bars = con.execute("SELECT * FROM bars WHERE symbol = ? ORDER BY bar_ts", [symbol]).fetchdf()
        if bars.empty:
            continue

        profiles = con.execute("""
            SELECT session_date, poc, vah, val FROM session_profile WHERE symbol = ?
        """, [symbol]).fetchdf()
        profiles_by_date = {
            pd.Timestamp(row["session_date"]).date(): {"poc": row["poc"], "vah": row["vah"], "val": row["val"]}
            for _, row in profiles.iterrows()
        }

        feats = compute_features_for_symbol(bars, profiles_by_date, tick_size, cfg)
        if feats.empty:
            continue

        rows = feats[[
            "symbol", "bar_ts", "cvd", "cvd_slope", "absorption_flag", "absorption_side",
            "distance_to_poc", "distance_to_val", "distance_to_vah", "adx", "regime", "rel_volume"
        ]].copy()
        rows["symbol"] = symbol
        # Vectorized insert via DataFrame — executemany() is row-by-row in DuckDB
        # and doesn't scale once you're processing weeks/months of bars.
        con.execute("""
            INSERT INTO features (symbol, bar_ts, cvd, cvd_slope, absorption_flag,
                                   absorption_side, distance_to_poc, distance_to_val,
                                   distance_to_vah, adx, regime, rel_volume)
            SELECT symbol, bar_ts, cvd, cvd_slope, absorption_flag, absorption_side,
                   distance_to_poc, distance_to_val, distance_to_vah, adx, regime, rel_volume
            FROM rows
        """)
        n_absorption = int(feats["absorption_flag"].sum())
        n_days = feats["bar_ts"].dt.date.nunique()
        print(f"  {symbol}: {len(feats)} bars processed across {n_days} session(s), "
              f"{n_absorption} absorption events flagged")


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    print("Running feature engine...")
    run_feature_engine(con, cfg)

    print("\nSample absorption events detected:")
    sample = con.execute("""
        SELECT symbol, bar_ts, absorption_side, distance_to_val, distance_to_vah, regime, rel_volume
        FROM features WHERE absorption_flag = TRUE
        ORDER BY bar_ts LIMIT 10
    """).fetchdf()
    print(sample.to_string(index=False))
    con.close()
