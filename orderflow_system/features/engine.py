"""
Core feature engine. Computes, per bar:
  - CVD (cumulative volume delta, session-anchored) and its slope
  - absorption flag + side (volume spike with minimal price movement)
  - distance to session POC / VAH / VAL (in ticks)
  - ADX + regime classification (trending vs ranging)
  - relative volume vs rolling average

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


def compute_features_for_symbol(bars: pd.DataFrame, profile_row: dict,
                                  tick_size: float, cfg: dict) -> pd.DataFrame:
    bars = bars.sort_values("bar_ts").reset_index(drop=True)
    feat_cfg = cfg["features"]

    # --- CVD: cumulative delta, anchored to session start (first bar of the day) ---
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

    # --- Distance to session levels (in ticks) ---
    if profile_row:
        bars["distance_to_poc"] = (bars["close"] - profile_row["poc"]) / tick_size
        bars["distance_to_val"] = (bars["close"] - profile_row["val"]) / tick_size
        bars["distance_to_vah"] = (bars["close"] - profile_row["vah"]) / tick_size
    else:
        bars["distance_to_poc"] = np.nan
        bars["distance_to_val"] = np.nan
        bars["distance_to_vah"] = np.nan

    # --- Regime via ADX ---
    bars["adx"] = compute_adx(bars, feat_cfg["regime_adx_period"])
    bars["regime"] = np.where(
        bars["adx"] >= feat_cfg["regime_adx_trend_threshold"], "trending", "ranging"
    )
    bars.loc[bars["adx"].isna(), "regime"] = None

    return bars


def run_feature_engine(con: duckdb.DuckDBPyConnection, cfg: dict):
    con.execute("DELETE FROM features")
    for sym_cfg in cfg["symbols"]:
        symbol = sym_cfg["name"]
        tick_size = sym_cfg["tick_size"]
        bars = con.execute("SELECT * FROM bars WHERE symbol = ? ORDER BY bar_ts", [symbol]).fetchdf()
        if bars.empty:
            continue

        session_date = bars["bar_ts"].dt.date.iloc[0]
        profile = con.execute("""
            SELECT poc, vah, val FROM session_profile
            WHERE symbol = ? AND session_date = ?
        """, [symbol, session_date]).fetchdf()
        profile_row = profile.iloc[0].to_dict() if not profile.empty else None

        feats = compute_features_for_symbol(bars, profile_row, tick_size, cfg)

        rows = feats[[
            "symbol", "bar_ts", "cvd", "cvd_slope", "absorption_flag", "absorption_side",
            "distance_to_poc", "distance_to_val", "distance_to_vah", "adx", "regime", "rel_volume"
        ]].copy()
        rows["symbol"] = symbol
        con.executemany("""
            INSERT INTO features (symbol, bar_ts, cvd, cvd_slope, absorption_flag,
                                   absorption_side, distance_to_poc, distance_to_val,
                                   distance_to_vah, adx, regime, rel_volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows.values.tolist())
        n_absorption = int(feats["absorption_flag"].sum())
        print(f"  {symbol}: {len(feats)} bars processed, {n_absorption} absorption events flagged")


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
