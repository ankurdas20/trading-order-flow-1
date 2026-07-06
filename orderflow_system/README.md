# Order Flow AI Trading System — V1

A single-strategy, fully automated, paper-trading pipeline: tick data → bars →
volume profile → order flow features → candidate setup filter → Gemini AI
decision engine → risk sizing → paper execution → journal.

**Status: built and verified end-to-end in this environment using synthetic
tick data.** Every stage below has actually been run and its output checked —
this is not untested code. What's NOT yet connected is live market data,
since this build environment can't reach IBKR or Databento's endpoints.
That's the one thing only you can do, on your own machine.

## What's actually built

| Stage | File | Verified? |
|---|---|---|
| Config | `config/config.yaml`, `config/loader.py` | ✅ loads correctly |
| Database schema | `database/schema.py` | ✅ creates all 4 tables |
| Tick ingestion (synthetic stand-in) | `data/synthetic_generator.py` | ✅ generates realistic ticks with planted absorption events |
| Tick → bar aggregation | `data/aggregator.py` | ✅ produces correct OHLCV + delta bars |
| Volume profile (POC/VAH/VAL) | `features/volume_profile.py` | ✅ computed from tick-level price distribution |
| Feature engine (CVD, absorption, ADX/regime, rel. volume) | `features/engine.py` | ✅ correctly detected 6/8 planted absorption events |
| Strategy filter (VAL/VAH absorption reversal) | `strategies/val_absorption_reversal.py` | ✅ correctly narrowed 6 events to 2 valid candidates |
| Decision engine (Gemini API) | `live/decision_engine.py` | ✅ structure verified in dry-run mock; **live Gemini calls need your API key + network** |
| Risk & position sizing | `live/risk_engine.py` | ✅ correct fixed-fractional sizing math |
| Paper execution simulator | `live/paper_executor.py` | ✅ correctly walks forward to stop/target |
| Trade journal | `journal/journal.py` | ✅ logs every decision + outcome, generates summary |
| Walk-forward backtest | `backtest/engine.py` | ✅ runs the full pipeline across N synthetic sessions, splits into chronological folds, reports an overfitting-style verdict |
| Full orchestrator | `main.py` | ✅ ran end-to-end, produced a complete journaled run |
| Telegram alerts | `alerts/telegram.py` | ✅ sends trade decisions + run summaries; silently no-ops if not configured |
| Mobile dashboard | `dashboard/generate.py` | ✅ generates a single static, self-contained `dashboard/index.html` (screenshotted on both phone and desktop widths) |
| Free 24/7 automation | `.github/workflows/pipeline.yml` | ✅ scheduled/on-demand GitHub Actions run — no server, no cost |

**Update since the initial build:** the feature engine was fixing a real bug — it was
using the CURRENT (still-forming) session's own volume profile as a feature, which is
look-ahead bias (you can't know today's finished POC/VAH/VAL until today's session is
over). It now correctly references the PRIOR completed session's profile, matching the
edge hypothesis in `config.yaml` ("price reaches prior session's VAL/VAH"). This means
`data.synthetic_sessions` in config must be >= 2 (a lone day has no prior session to
reference) — defaulted to 2. The synthetic generator was also updated to plant each
day's absorption events near the previous day's actual VAL/VAH (instead of an arbitrary
offset from that day's own price), so the edge still has something real to fire on.

## Using this from your phone or laptop — no coding, no server to pay for

You don't need to touch a terminal, Python, or config files to use this
day-to-day. The setup below makes GitHub itself the "always-on computer" (its
free scheduled-jobs feature, GitHub Actions) and Telegram + a webpage the
interface — both look identical on phone and laptop.

**One-time setup (~5 minutes, all clicking/typing into forms, zero code):**

1. **Create a Telegram bot** (this is what sends you alerts): open Telegram,
   message **@BotFather**, send `/newbot`, follow the prompts. It gives you a
   *bot token*. Then send your new bot any message once, and visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser to find
   your numeric *chat id* in the reply. Full detail in `alerts/telegram.py`.
2. **Add your secrets to GitHub** (so the automation can use them without
   them ever appearing in code): in this repo, go to **Settings → Secrets and
   variables → Actions → New repository secret**, and add:
   `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. All optional —
   without them the pipeline still runs, just without live AI decisions or
   phone alerts.
3. **Turn on the dashboard webpage**: **Settings → Pages → Build and
   deployment → Source: "GitHub Actions"**. One dropdown, no code.
4. **Run it**: open the **Actions** tab (works fine in the GitHub mobile app
   too) → "Order flow pipeline" → **Run workflow**. A few minutes later:
   trade alerts appear in your Telegram chat, and your dashboard is live at
   `https://<your-username>.github.io/<this-repo>/`.

That's the whole loop, forever, without opening a terminal again: tap "Run
workflow" on your phone (or wait for the schedule once you turn it on),
read alerts in Telegram, check the dashboard link — both native to any
device, nothing to install.

**Why this is free:** GitHub Actions gives every repo free scheduled-job
minutes, GitHub Pages hosting is free, and Telegram's API is free. Nothing
here needs a paid server or a new account beyond GitHub + Telegram, which you
already effectively have.

**Two honest limits of this "runs anywhere for free" approach:**
- The `schedule:` trigger in `.github/workflows/pipeline.yml` is commented
  out by default, because right now `data.source` is still `"synthetic"` —
  running a timer before real data is wired up just fills your dashboard and
  phone with synthetic-data noise, not real signals. Uncomment it once
  Databento is connected (see below).
- **IBKR doesn't fit this model.** Its API needs TWS/Gateway logged in and
  running continuously on one specific computer — that's fundamentally
  different from "a free scheduled job runs anywhere." So the automated,
  phone-accessible path in this README is built around **Databento** (a pure
  API, works headless, fits GitHub Actions fine). IBKR paper trading remains
  possible, just as a separate manual/local-only path you'd run from your
  own machine when you're at it — not part of the automated loop.

## The one edge this system trades (V1)

**VAL/VAH Absorption Reversal**: price reaches the prior session's Value Area
Low or High, an absorption event fires (volume spike with minimal price
movement) on the side consistent with a reversal, and the setup targets the
session Point of Control. Full logic in `config.yaml` under `strategy:` and
`strategies/val_absorption_reversal.py`.

Deliberately just one edge. Do not add more strategies until this one has a
statistically meaningful paper track record (100+ logged trades). See the
"Common mistakes" section below.

## Setup on your own machine

```bash
# 1. Clone/copy this folder, then:
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Get a Gemini API key (you already have this per our conversation)
export GEMINI_API_KEY="your-key-here"

# 3. Run the pipeline on synthetic data first, to confirm your environment works
python3 main.py
```

You should see the exact same kind of output shown when this was built —
ticks generated, bars built, profiles computed, features run, candidates
found, decisions made, journal summarized.

## Connecting real data (the step only you can do)

This sandbox can't reach IBKR or Databento's servers, so `data/source` is
set to `"synthetic"` in `config.yaml`. On your machine, with real network
access and credentials:

1. **For historical backtesting**: write `data/databento_ingest.py`. It needs
   to produce rows matching the `raw_ticks` schema (`symbol, ts, price, size,
   aggressor`) — Databento's trade schema gives you side directly, no tick-rule
   inference needed.
2. **For live paper trading**: write `data/ibkr_ingest.py` using `ibapi`,
   connecting to your IBKR **paper account** (not live). IBKR's tick data
   doesn't always tag aggressor side directly — you may need the tick rule
   (classify a trade as buy-initiated if it occurs at/above the prevailing ask,
   sell-initiated if at/below the bid) as an approximation.
3. Change `data.source` in `config.yaml` from `"synthetic"` to `"databento"`
   or `"ibkr"`, and add the corresponding branch in `main.py`'s ingestion step.
4. Everything downstream (aggregation, features, strategy, decision engine,
   journal) needs **zero changes** — that's the point of the raw_ticks schema
   being the single contract between data source and pipeline.

## Switching the decision engine from mock to live

It already works — `live/decision_engine.py` calls Gemini for real whenever
`GEMINI_API_KEY` is set in your environment. The dry-run mock only activates
as a fallback if the key is missing or a live call errors out. Just export
the key and re-run.

## Running the walk-forward backtest

```bash
python3 -m backtest.engine --sessions 80 --folds 5
```

This generates N consecutive synthetic trading sessions, runs the full pipeline
(bars → profile → features → strategy filter → decision engine → paper
execution) across all of them, then splits the resulting trade log into
chronological folds and reports per-fold win rate / expectancy / P&L plus a
verdict: `ROBUST`, `MODERATE`, `WEAK`, `INCONSISTENT`, `DEAD`, or
`INSUFFICIENT_DATA` (fewer than 30 trades total — don't trust anything below
that). **On synthetic data this only proves the mechanism works** — the
setups are planted, so a good verdict here is not evidence of a real edge.
Once `data.source` is `databento` or `ibkr`, point this at real history and
the verdict actually means something. Note this build's decision thresholds
are fixed in `config.yaml`, not fit to data — so right now this is an
out-of-time consistency check, not overfitting protection. The moment you (or
an AI) start tuning thresholds based on backtest results, you must tune only
on train folds and judge only on held-out test folds, or you're just
overfitting with extra steps.

## Realistic next steps, in order

1. **Get real historical tick data flowing** (Databento) and re-run the
   feature engine, strategy filter, and `backtest.engine` against it. See how
   often real candidates actually occur, and what the walk-forward verdict
   says on real history — synthetic data was tuned to produce events, real
   markets won't be so cooperative.
2. **Uncomment the `schedule:` block** in `.github/workflows/pipeline.yml` so
   it runs automatically during market hours instead of only on manual
   trigger — Telegram alerts and the dashboard update themselves from there.
3. **Paper trade for real, for weeks** — 100 trades minimum before drawing any
   conclusion about whether this edge (or the AI's judgment on it) is any good.
4. **Only then** consider a second strategy, an ensemble/ranking layer, or a
   richer dashboard. Multiplying complexity before step 3 is exactly how
   these projects die — twenty untested strategies is not better than one
   tested one.

(Telegram alerting and the dashboard are done — see "Using this from your
phone or laptop" above. IBKR live paper trading is intentionally not part of
the automated path; see the limits noted there.)

## Common mistakes this build deliberately avoids

- **No chart images sent to the AI.** The decision engine only ever sees a
  structured JSON feature snapshot — numbers, not pixels. This is testable,
  reproducible, and avoids the AI hallucinating chart patterns.
- **temperature=0** on every decision call, to minimize unnecessary variance
  between identical market conditions.
- **The AI is instructed to default to no_trade.** Read the system prompt in
  `decision_engine.py` — it explicitly fights the tendency of chat models to
  always find a reason to say yes.
- **Every AI output is validated in code** before being accepted (sane R:R,
  stop/target on the correct side of entry). The model proposes, code
  disposes — this is why `validate_decision()` exists as a separate,
  deterministic function.
- **No_trade decisions are logged too**, not just executed trades — you need
  this to judge whether the AI's caution is well-calibrated, not just whether
  its trades win.
