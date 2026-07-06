"""
Converts a validated TradeDecision into an actual position size, using
fixed fractional risk against the paper account size. Independent of the
decision engine on purpose — the AI decides direction/levels, code decides
size. Keeps the one part of the system with real capital-preservation
consequences fully deterministic and auditable.
"""
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from live.decision_engine import TradeDecision


def size_position(decision: TradeDecision, symbol: str, cfg: dict) -> dict:
    if decision.trade == "no_trade":
        return {"contracts": 0, "risk_dollars": 0.0}

    sym_cfg = next(s for s in cfg["symbols"] if s["name"] == symbol)
    point_value = sym_cfg["point_value"]

    account_size = cfg["risk"]["account_size_paper"]
    risk_pct = cfg["risk"]["risk_per_trade_pct"] / 100.0
    max_risk_dollars = account_size * risk_pct

    risk_points = abs(decision.entry - decision.stop_loss)
    risk_dollars_per_contract = risk_points * point_value

    if risk_dollars_per_contract <= 0:
        return {"contracts": 0, "risk_dollars": 0.0}

    contracts = int(max_risk_dollars // risk_dollars_per_contract)
    contracts = max(contracts, 1) if max_risk_dollars >= risk_dollars_per_contract else 0

    actual_risk = contracts * risk_dollars_per_contract
    potential_reward = contracts * abs(decision.take_profit - decision.entry) * point_value

    return {
        "contracts": contracts,
        "risk_dollars": round(actual_risk, 2),
        "potential_reward_dollars": round(potential_reward, 2),
        "max_allowed_risk_dollars": round(max_risk_dollars, 2),
    }


if __name__ == "__main__":
    cfg = load_config()
    # Quick manual test with a fabricated decision
    fake_decision = TradeDecision(
        trade="long", entry=20000.0, stop_loss=19995.0, take_profit=20015.0,
        risk_reward=3.0, confidence=70, reasoning="test"
    )
    sizing = size_position(fake_decision, "NQ", cfg)
    print("Sizing result for test NQ long (entry=20000, stop=19995, target=20015):")
    for k, v in sizing.items():
        print(f"  {k}: {v}")
