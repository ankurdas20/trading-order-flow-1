"""
DuckDB schema for the order flow system.

Tables:
  raw_ticks       - every tick, tagged with aggressor side
  bars            - aggregated OHLCV + delta bars (built from raw_ticks)
  features        - computed features per bar (CVD, absorption, regime, etc.)
  session_profile - per-session volume profile (POC, VAH, VAL)
"""
import duckdb
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config


def init_db(db_path: str) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw_ticks (
            symbol      VARCHAR NOT NULL,
            ts          TIMESTAMP NOT NULL,
            price       DOUBLE NOT NULL,
            size        INTEGER NOT NULL,
            aggressor   VARCHAR NOT NULL  -- 'buy' or 'sell', inferred via tick rule if not provided
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            symbol       VARCHAR NOT NULL,
            bar_ts       TIMESTAMP NOT NULL,   -- bar open time
            open         DOUBLE NOT NULL,
            high         DOUBLE NOT NULL,
            low          DOUBLE NOT NULL,
            close        DOUBLE NOT NULL,
            volume       INTEGER NOT NULL,
            buy_volume   INTEGER NOT NULL,
            sell_volume  INTEGER NOT NULL,
            delta        INTEGER NOT NULL,     -- buy_volume - sell_volume
            PRIMARY KEY (symbol, bar_ts)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS features (
            symbol              VARCHAR NOT NULL,
            bar_ts              TIMESTAMP NOT NULL,
            cvd                 DOUBLE,          -- cumulative volume delta (rolling window)
            cvd_slope           DOUBLE,          -- rate of change of CVD over lookback
            absorption_flag     BOOLEAN,         -- high volume, low price movement
            absorption_side     VARCHAR,         -- 'buy_absorption' / 'sell_absorption' / null
            distance_to_poc     DOUBLE,          -- price distance to session POC (in ticks)
            distance_to_val     DOUBLE,
            distance_to_vah     DOUBLE,
            adx                 DOUBLE,
            regime              VARCHAR,         -- 'trending' / 'ranging'
            rel_volume          DOUBLE,          -- volume vs rolling average
            PRIMARY KEY (symbol, bar_ts)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS session_profile (
            symbol       VARCHAR NOT NULL,
            session_date DATE NOT NULL,
            poc          DOUBLE,
            vah          DOUBLE,
            val          DOUBLE,
            PRIMARY KEY (symbol, session_date)
        )
    """)

    return con


if __name__ == "__main__":
    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    tables = con.execute("SHOW TABLES").fetchall()
    print("Initialized DuckDB with tables:", [t[0] for t in tables])
    con.close()
