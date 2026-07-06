"""
Decision engine. Takes ONE candidate setup (already filtered by the strategy
layer — this never sees raw, unfiltered market data) and asks Gemini to
produce a full, structured trade plan: direction, entry, stop, target, R:R,
confidence, and reasoning.

Design choices that matter:
  - temperature=0.0 (set in config) to minimize decision variance for the
    same market conditions.
  - Structured JSON output only — never free-text — so every decision is
    loggable and statistically analyzable.
  - The model is instructed to default to no_trade. An LLM asked "should I
    trade" will find a reason to say yes far too often; the prompt actively
    fights that tendency.
  - Every output is validated in code (sane R:R, stop/target on the correct
    side of entry) before being accepted. The model proposes; code disposes.

Requires env var GEMINI_API_KEY. If not set, runs in DRY-RUN mode using a
rule-based mock so you can test the full pipeline (validation, logging,
journal) without needing network access or a live key.
"""
import os
import json
import sys
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config

SYSTEM_PROMPT_TEMPLATE = """You are a disciplined risk manager evaluating a single CME futures trade \
setup for a PAPER TRADING system. No real money is at risk, but you must still reason rigorously — \
sloppy reasoning here produces a misleading paper track record.

STRATEGY PLAYBOOK (the only edge this system trades):
{strategy_description}

You will be given a structured feature snapshot for ONE candidate setup that has ALREADY passed a \
structural filter (it is near a key value area level and an absorption event fired on the matching side). \
Your job is NOT to find a trade — the candidate already exists. Your job is to judge whether the evidence \
in the snapshot is actually strong enough to justify a trade, or whether it is weak/ambiguous/contradicted \
by other signals (e.g. regime, CVD slope direction, weak relative volume).

Rules:
- Default to "no_trade" unless the evidence is genuinely convincing. Being conservative is correct here.
- If you recommend a trade, risk_reward must be >= {min_rr}.
- stop_loss must be placed beyond the absorption zone with a buffer of approximately {stop_buffer_ticks} \
ticks (tick size = {tick_size}).
- take_profit should target the session POC (point of control) as the primary reference, adjusted to \
satisfy the minimum R:R.
- reasoning must be one or two sentences, specific to the numbers you were given — not generic.

Respond ONLY with valid JSON matching this exact schema, no other text:
{{
  "trade": "long" | "short" | "no_trade",
  "entry": number or null,
  "stop_loss": number or null,
  "take_profit": number or null,
  "risk_reward": number or null,
  "confidence": integer 0-100,
  "reasoning": string
}}
"""


@dataclass
class TradeDecision:
    trade: str
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    risk_reward: Optional[float]
    confidence: int
    reasoning: str
    valid: bool = True
    validation_note: str = ""


def build_system_prompt(cfg: dict) -> str:
    strat = cfg["strategy"]
    sym_cfg = cfg["symbols"][0]  # tick_size passed per-call in practice; template default here
    return SYSTEM_PROMPT_TEMPLATE.format(
        strategy_description=strat["description"].strip(),
        min_rr=strat["min_risk_reward"],
        stop_buffer_ticks=strat["stop_buffer_ticks"],
        tick_size=sym_cfg["tick_size"],
    )


def build_user_payload(candidate_row: dict) -> str:
    """Feature snapshot sent to the model — numbers only, no raw ticks/charts."""
    snapshot = {
        "symbol": candidate_row["symbol"],
        "candidate_direction": candidate_row["candidate_direction"],
        "close": candidate_row["close"],
        "distance_to_val_ticks": candidate_row["distance_to_val"],
        "distance_to_vah_ticks": candidate_row["distance_to_vah"],
        "distance_to_poc_ticks": candidate_row["distance_to_poc"],
        "absorption_side": candidate_row["absorption_side"],
        "regime": candidate_row["regime"],
        "adx": round(candidate_row["adx"], 1) if candidate_row["adx"] is not None else None,
        "relative_volume": round(candidate_row["rel_volume"], 2),
        "cvd_slope": candidate_row["cvd_slope"],
        "delta_this_bar": candidate_row["delta"],
    }
    return json.dumps(snapshot, indent=2)


def call_gemini(system_prompt: str, user_payload: str, cfg: dict) -> dict:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    dec_cfg = cfg["decision_engine"]

    response = client.models.generate_content(
        model=dec_cfg["model"],
        contents=user_payload,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=dec_cfg["temperature"],
            max_output_tokens=dec_cfg["max_output_tokens"],
            response_mime_type="application/json",
        ),
    )
    return json.loads(response.text)


def mock_decision(candidate_row: dict, cfg: dict) -> dict:
    """
    DRY-RUN fallback — used when GEMINI_API_KEY is not set or network is
    unavailable (e.g. this sandbox). Implements a simple deterministic rule
    so the rest of the pipeline (validation, journaling) can be tested now.
    Replace with a real call_gemini() run once you have network + API key.
    """
    direction = candidate_row["candidate_direction"]
    tick_size = 0.25  # NQ default for this mock
    close = candidate_row["close"]
    poc_dist = candidate_row["distance_to_poc"]

    strong_evidence = (
        candidate_row["rel_volume"] >= 3.0 and
        candidate_row["regime"] in ("ranging", "trending")
    )

    if not strong_evidence:
        return {
            "trade": "no_trade", "entry": None, "stop_loss": None,
            "take_profit": None, "risk_reward": None, "confidence": 30,
            "reasoning": "[MOCK] Relative volume insufficient for high-confidence absorption call."
        }

    stop_ticks = cfg["strategy"]["stop_buffer_ticks"]
    if direction == "long":
        entry = close
        stop = entry - stop_ticks * tick_size
        target = entry + abs(poc_dist) * tick_size if poc_dist and poc_dist > 0 else entry + stop_ticks * tick_size * 3
    else:
        entry = close
        stop = entry + stop_ticks * tick_size
        target = entry - abs(poc_dist) * tick_size if poc_dist and poc_dist < 0 else entry - stop_ticks * tick_size * 3

    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0

    return {
        "trade": direction if rr >= cfg["strategy"]["min_risk_reward"] else "no_trade",
        "entry": round(entry, 2), "stop_loss": round(stop, 2), "take_profit": round(target, 2),
        "risk_reward": rr, "confidence": 65,
        "reasoning": f"[MOCK] {direction} absorption near value area edge with {candidate_row['rel_volume']:.1f}x volume; targeting POC."
    }


def validate_decision(raw: dict, cfg: dict) -> TradeDecision:
    d = TradeDecision(
        trade=raw.get("trade", "no_trade"),
        entry=raw.get("entry"), stop_loss=raw.get("stop_loss"),
        take_profit=raw.get("take_profit"), risk_reward=raw.get("risk_reward"),
        confidence=int(raw.get("confidence", 0)), reasoning=raw.get("reasoning", "")
    )

    if d.trade == "no_trade":
        return d

    min_rr = cfg["strategy"]["min_risk_reward"]
    if d.entry is None or d.stop_loss is None or d.take_profit is None:
        d.valid, d.trade, d.validation_note = False, "no_trade", "Missing required price levels; forced no_trade."
        return d

    if d.trade == "long" and not (d.stop_loss < d.entry < d.take_profit):
        d.valid, d.trade, d.validation_note = False, "no_trade", "Long levels out of order (stop/entry/target); forced no_trade."
        return d
    if d.trade == "short" and not (d.take_profit < d.entry < d.stop_loss):
        d.valid, d.trade, d.validation_note = False, "no_trade", "Short levels out of order; forced no_trade."
        return d

    risk = abs(d.entry - d.stop_loss)
    reward = abs(d.take_profit - d.entry)
    actual_rr = round(reward / risk, 2) if risk > 0 else 0
    if actual_rr < min_rr:
        d.valid, d.trade, d.validation_note = False, "no_trade", f"R:R {actual_rr} below minimum {min_rr}; forced no_trade."
        return d

    d.risk_reward = actual_rr
    return d


def decide(candidate_row: dict, cfg: dict) -> TradeDecision:
    use_live = bool(os.environ.get("GEMINI_API_KEY")) and cfg["decision_engine"].get("use_live_api", True)
    if use_live:
        try:
            system_prompt = build_system_prompt(cfg)
            user_payload = build_user_payload(candidate_row)
            raw = call_gemini(system_prompt, user_payload, cfg)
        except Exception as e:
            print(f"  [WARN] Live Gemini call failed ({e}); falling back to dry-run mock for this candidate.")
            raw = mock_decision(candidate_row, cfg)
    else:
        raw = mock_decision(candidate_row, cfg)

    return validate_decision(raw, cfg)


if __name__ == "__main__":
    import duckdb
    from database.schema import init_db
    from strategies.val_absorption_reversal import find_candidates

    cfg = load_config()
    con = init_db(cfg["database"]["path"])
    candidates = find_candidates(con)

    if candidates.empty:
        print("No candidates found to evaluate.")
    else:
        print(f"Evaluating {len(candidates)} candidate(s) with decision engine "
              f"({'LIVE' if os.environ.get('GEMINI_API_KEY') else 'DRY-RUN MOCK'} mode)...\n")
        for _, row in candidates.iterrows():
            decision = decide(row.to_dict(), cfg)
            print(f"  {row['symbol']} @ {row['bar_ts']} ({row['candidate_direction']}) -> "
                  f"{decision.trade.upper()}"
                  + (f"  entry={decision.entry} stop={decision.stop_loss} "
                     f"target={decision.take_profit} R:R={decision.risk_reward} "
                     f"confidence={decision.confidence}" if decision.trade != "no_trade" else ""))
            print(f"    reasoning: {decision.reasoning}")
            if decision.validation_note:
                print(f"    validation: {decision.validation_note}")
    con.close()
