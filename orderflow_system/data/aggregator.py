"""
Aggregates raw_ticks into bars with OHLCV + buy/sell volume + delta.
This is where "order flow" actually starts to become visible: a plain OHLCV
bar throws away who was aggressing (buyer or seller); this keeps it.
"""
import duckdb
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from database.schema import init_db


def build_bars(con: duckdb.DuckDBPyConnection, bar_interval_seconds: int = 60):
    con.execute("DELETE FROM bars")
    con.execute(f"""
        INSERT INTO bars
        SELECT
            symbol,
            time_bucket(INTERVAL '{bar_interval_seconds} seconds', ts) AS bar_ts,
            FIRST(price ORDER BY ts) AS open,
            MAX(price) AS high,
            MIN(price) AS low,
            LAST(price ORDER BY ts) AS close,
            SUM(size) AS volume,
            SUM(CASE WHEN aggressor = 'buy' THEN size ELSE 0 END) AS buy_volume,
            SUM(CASE WHEN aggressor = 'sell' THEN size ELSE 0 END) AS sell_volume,
            SUM(CASE WHEN aggressor = 'buy' THEN size ELSE -size END) AS delta
        FROM raw_ticks
        GROUP BY symbol, time_bucket(INTERVAL '{bar_interval_seconds} seconds', ts)
        ORDER BY symbol, bar_ts
    """)


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    build_bars(con, cfg["data"]["bar_interval_seconds"])
    n_bars = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    print(f"Built {n_bars} bars")
    sample = con.execute("""
        SELECT symbol, bar_ts, open, high, low, close, volume, delta
        FROM bars ORDER BY symbol, bar_ts LIMIT 5
    """).fetchdf()
    print(sample.to_string(index=False))
    con.close()
