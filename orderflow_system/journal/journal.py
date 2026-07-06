"""
Trade journal. Every decision the engine makes gets logged here — including
no_trade decisions, since "the AI correctly stayed out" is just as important
a data point as a winning trade when you're evaluating whether this thing
actually works.
"""
import duckdb
from pathlib import Path
import sys
import json

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config


def init_journal(path: str) -> duckdb.DuckDBPyConnection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id                  INTEGER PRIMARY KEY,
            logged_at           TIMESTAMP DEFAULT current_timestamp,
            symbol              VARCHAR,
            bar_ts              TIMESTAMP,
            candidate_direction VARCHAR,
            feature_snapshot    VARCHAR,   -- JSON string of the feature snapshot sent to the model
            trade               VARCHAR,   -- long / short / no_trade
            entry               DOUBLE,
            stop_loss           DOUBLE,
            take_profit         DOUBLE,
            risk_reward         DOUBLE,
            confidence          INTEGER,
            reasoning           VARCHAR,
            validation_note     VARCHAR,
            contracts           INTEGER,
            risk_dollars        DOUBLE,
            outcome             VARCHAR,   -- stop / target / timeout / no_data / null (if no_trade)
            exit_price          DOUBLE,
            exit_ts             TIMESTAMP,
            pnl_dollars         DOUBLE
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS decision_id_seq START 1")
    return con


def log_decision(con: duckdb.DuckDBPyConnection, symbol, bar_ts, candidate_direction,
                  feature_snapshot: dict, decision, sizing: dict,
                  outcome: dict | None, point_value: float) -> int:
    pnl = None
    if outcome and decision.trade != "no_trade" and sizing.get("contracts", 0) > 0:
        direction_mult = 1 if decision.trade == "long" else -1
        pnl = round(
            direction_mult * (outcome["exit_price"] - decision.entry) * sizing["contracts"] * point_value, 2
        )

    next_id = con.execute("SELECT nextval('decision_id_seq')").fetchone()[0]
    con.execute("""
        INSERT INTO decisions (
            id, symbol, bar_ts, candidate_direction, feature_snapshot,
            trade, entry, stop_loss, take_profit, risk_reward, confidence,
            reasoning, validation_note, contracts, risk_dollars,
            outcome, exit_price, exit_ts, pnl_dollars
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        next_id, symbol, bar_ts, candidate_direction, json.dumps(feature_snapshot),
        decision.trade, decision.entry, decision.stop_loss, decision.take_profit,
        decision.risk_reward, decision.confidence, decision.reasoning, decision.validation_note,
        sizing.get("contracts", 0), sizing.get("risk_dollars", 0.0),
        outcome.get("outcome") if outcome else None,
        outcome.get("exit_price") if outcome else None,
        outcome.get("exit_ts") if outcome else None,
        pnl
    ])
    return next_id


def summary_report(con: duckdb.DuckDBPyConnection):
    total = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    traded = con.execute("SELECT COUNT(*) FROM decisions WHERE trade != 'no_trade'").fetchone()[0]
    wins = con.execute("SELECT COUNT(*) FROM decisions WHERE outcome = 'target'").fetchone()[0]
    losses = con.execute("SELECT COUNT(*) FROM decisions WHERE outcome = 'stop'").fetchone()[0]
    total_pnl = con.execute("SELECT COALESCE(SUM(pnl_dollars), 0) FROM decisions").fetchone()[0]
    avg_conf_traded = con.execute("SELECT AVG(confidence) FROM decisions WHERE trade != 'no_trade'").fetchone()[0]

    print("=== Journal Summary ===")
    print(f"  Total decisions logged: {total}")
    print(f"  Trades taken: {traded}  (no_trade: {total - traded})")
    print(f"  Wins (target hit): {wins}  Losses (stop hit): {losses}")
    if traded > 0:
        win_rate = wins / traded * 100
        print(f"  Win rate: {win_rate:.1f}%")
        print(f"  Avg confidence on traded setups: {avg_conf_traded:.1f}")
    print(f"  Total paper P&L: ${total_pnl:.2f}")


if __name__ == "__main__":
    cfg = load_config()
    con = init_journal(cfg["journal"]["path"])
    print("Journal initialized at", cfg["journal"]["path"])
    summary_report(con)
    con.close()
