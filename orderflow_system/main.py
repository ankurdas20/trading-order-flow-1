"""
Main orchestrator. One command runs the entire pipeline end to end:

  1. Ingest tick data (synthetic for now; swap for IBKR/Databento on your machine)
  2. Aggregate ticks -> bars
  3. Compute session volume profile (POC/VAH/VAL)
  4. Run feature engine (CVD, absorption, regime, relative volume)
  5. Filter to candidate setups (strategy layer)
  6. Send each candidate to the decision engine (Gemini, or dry-run mock)
  7. Size the position (risk engine)
  8. Simulate paper execution (or route to IBKR paper account, once wired up)
  9. Log everything to the journal
  10. Print a summary report

Usage:
    python3 main.py
"""
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from config.loader import load_config
from database.schema import init_db
from data.synthetic_generator import load_synthetic_data
from data.aggregator import build_bars
from features.volume_profile import update_all_session_profiles
from features.engine import run_feature_engine
from strategies.val_absorption_reversal import find_candidates
from live.decision_engine import decide
from live.risk_engine import size_position
from live.paper_executor import simulate_execution
from journal.journal import init_journal, log_decision, summary_report


def run_pipeline():
    cfg = load_config()
    print("=" * 60)
    print("ORDER FLOW SYSTEM — FULL PIPELINE RUN")
    print(f"Data source: {cfg['data']['source']}  |  "
          f"Decision engine: {'LIVE' if os.environ.get('GEMINI_API_KEY') else 'DRY-RUN MOCK'}")
    print("=" * 60)

    con = init_db(cfg["database"]["path"])

    print("\n[1/6] Ingesting tick data...")
    if cfg["data"]["source"] == "synthetic":
        con.execute("DELETE FROM raw_ticks")
        load_synthetic_data(con, cfg)
    else:
        raise NotImplementedError(
            f"Data source '{cfg['data']['source']}' not wired up in this environment. "
            f"Implement data/{cfg['data']['source']}_ingest.py on your own machine with "
            f"real credentials, matching the raw_ticks table schema."
        )

    print("\n[2/6] Aggregating ticks into bars...")
    build_bars(con, cfg["data"]["bar_interval_seconds"])

    print("\n[3/6] Computing session volume profiles...")
    update_all_session_profiles(con, cfg)

    print("\n[4/6] Running feature engine...")
    run_feature_engine(con, cfg)

    print("\n[5/6] Filtering candidate setups...")
    candidates = find_candidates(con)
    print(f"  {len(candidates)} candidate(s) found")

    print("\n[6/6] Running decision engine + paper execution + journaling...")
    journal_con = init_journal(cfg["journal"]["path"])

    if candidates.empty:
        print("  No candidates to evaluate this run.")
    else:
        for _, row in candidates.iterrows():
            row_dict = row.to_dict()
            symbol = row_dict["symbol"]
            sym_cfg = next(s for s in cfg["symbols"] if s["name"] == symbol)

            decision = decide(row_dict, cfg)
            sizing = size_position(decision, symbol, cfg)

            outcome = None
            if decision.trade != "no_trade" and sizing["contracts"] > 0:
                outcome = simulate_execution(con, symbol, row_dict["bar_ts"], decision)

            log_decision(
                journal_con, symbol, row_dict["bar_ts"], row_dict["candidate_direction"],
                feature_snapshot={k: v for k, v in row_dict.items() if k not in ("bar_ts",)},
                decision=decision, sizing=sizing, outcome=outcome,
                point_value=sym_cfg["point_value"]
            )

            status = decision.trade.upper()
            print(f"  {symbol} @ {row_dict['bar_ts']}: {status}", end="")
            if decision.trade != "no_trade":
                print(f"  entry={decision.entry} stop={decision.stop_loss} target={decision.take_profit} "
                      f"R:R={decision.risk_reward} contracts={sizing['contracts']}", end="")
                if outcome:
                    print(f"  -> {outcome['outcome']} @ {outcome['exit_price']}", end="")
            print()
            print(f"    reasoning: {decision.reasoning}")

    print()
    summary_report(journal_con)

    con.close()
    journal_con.close()


if __name__ == "__main__":
    run_pipeline()
