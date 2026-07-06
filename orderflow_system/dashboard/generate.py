"""
Generates a single self-contained dashboard/index.html from the trade
journal — no server, no external JS/CSS dependency (so it still renders if
GitHub Pages or your browser has no network access), viewable from any
phone/laptop browser by just opening the file or visiting the published URL.

Run manually:
    python3 -m dashboard.generate

The GitHub Actions workflow (.github/workflows/pipeline.yml) runs this after
every scheduled pipeline run and publishes the result to GitHub Pages, so the
dashboard is always current without you doing anything.
"""
import sys
from pathlib import Path
from html import escape

import duckdb
import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))
from config.loader import load_config
from journal.journal import get_summary_stats

OUT_PATH = Path(__file__).parent / "index.html"


def _equity_curve_svg(pnl_series: pd.Series, width: int = 760, height: int = 220) -> str:
    if pnl_series.empty:
        return '<p class="muted">No trades yet — nothing to chart.</p>'

    cum = pnl_series.cumsum()
    values = [0.0] + cum.tolist()
    n = len(values)
    vmin, vmax = min(values), max(values)
    vrange = (vmax - vmin) or 1.0
    pad = 20

    def x(i):
        return pad + i * (width - 2 * pad) / max(n - 1, 1)

    def y(v):
        return height - pad - (v - vmin) * (height - 2 * pad) / vrange

    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    zero_y = y(0.0)
    line_color = "#2ecc71" if values[-1] >= 0 else "#e74c3c"

    return f"""
    <svg viewBox="0 0 {width} {height}" class="equity-svg" preserveAspectRatio="xMidYMid meet">
      <line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" y2="{zero_y:.1f}"
            stroke="currentColor" stroke-opacity="0.25" stroke-dasharray="4 4" />
      <polyline points="{points}" fill="none" stroke="{line_color}" stroke-width="2.5" />
    </svg>
    """


def _table_rows(df: pd.DataFrame) -> str:
    if df.empty:
        return '<tr><td colspan="8" class="muted">No decisions logged yet.</td></tr>'
    rows = []
    for _, r in df.iterrows():
        pnl = r["pnl_dollars"]
        has_pnl = pd.notna(pnl)
        pnl_class = "pos" if has_pnl and pnl > 0 else ("neg" if has_pnl and pnl < 0 else "")
        pnl_text = f"${pnl:,.2f}" if has_pnl else "—"
        outcome_text = str(r["outcome"]) if pd.notna(r["outcome"]) else "—"
        reasoning_text = str(r["reasoning"]) if pd.notna(r["reasoning"]) else ""
        rows.append(f"""<tr>
            <td>{escape(str(r['bar_ts']))}</td>
            <td>{escape(str(r['symbol']))}</td>
            <td>{escape(str(r['trade']))}</td>
            <td>{escape(outcome_text)}</td>
            <td>{r['confidence']}</td>
            <td class="{pnl_class}">{pnl_text}</td>
            <td class="muted">{escape(reasoning_text)}</td>
        </tr>""")
    return "\n".join(rows)


def _symbol_breakdown_rows(df: pd.DataFrame) -> str:
    traded = df[df["trade"] != "no_trade"]
    if traded.empty:
        return '<tr><td colspan="4" class="muted">No trades yet.</td></tr>'
    rows = []
    for symbol, g in traded.groupby("symbol"):
        n = len(g)
        wins = int((g["outcome"] == "target").sum())
        pnl = g["pnl_dollars"].fillna(0).sum()
        win_rate = wins / n * 100 if n else 0
        rows.append(f"""<tr>
            <td>{escape(symbol)}</td><td>{n}</td>
            <td>{win_rate:.1f}%</td><td>${pnl:,.2f}</td>
        </tr>""")
    return "\n".join(rows)


def generate_dashboard(journal_path: str, out_path: Path = OUT_PATH) -> Path:
    con = duckdb.connect(journal_path, read_only=True)
    stats = get_summary_stats(con)
    df = con.execute("SELECT * FROM decisions ORDER BY bar_ts").df()
    con.close()

    traded_df = df[df["trade"] != "no_trade"] if not df.empty else df
    equity_svg = _equity_curve_svg(traded_df["pnl_dollars"].fillna(0)) if not traded_df.empty else _equity_curve_svg(pd.Series(dtype=float))
    recent_rows = _table_rows(df.tail(25).iloc[::-1])
    symbol_rows = _symbol_breakdown_rows(df)

    pnl_class = "pos" if stats["total_pnl"] > 0 else ("neg" if stats["total_pnl"] < 0 else "")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Order Flow System — Dashboard</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 1.5rem; max-width: 900px; margin-inline: auto;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: Canvas; color: CanvasText;
  }}
  h1 {{ font-size: 1.3rem; margin-bottom: 0.25rem; }}
  .subtitle {{ opacity: 0.6; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  .stat-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 0.75rem; margin-bottom: 1.5rem;
  }}
  .stat-tile {{
    border: 1px solid color-mix(in srgb, CanvasText 15%, transparent);
    border-radius: 10px; padding: 0.75rem 1rem;
  }}
  .stat-tile .label {{ font-size: 0.72rem; opacity: 0.6; text-transform: uppercase; letter-spacing: 0.03em; }}
  .stat-tile .value {{ font-size: 1.4rem; font-weight: 600; margin-top: 0.15rem; }}
  section {{ margin-bottom: 2rem; }}
  section h2 {{ font-size: 1rem; opacity: 0.85; margin-bottom: 0.6rem; }}
  .equity-svg {{ width: 100%; height: auto; color: CanvasText; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid color-mix(in srgb, CanvasText 10%, transparent); }}
  th {{ opacity: 0.6; font-weight: 500; font-size: 0.72rem; text-transform: uppercase; }}
  .pos {{ color: #2ecc71; }}
  .neg {{ color: #e74c3c; }}
  .muted {{ opacity: 0.5; }}
  .table-wrap {{ overflow-x: auto; }}
</style>
</head>
<body>
  <h1>Order Flow System — Dashboard</h1>
  <div class="subtitle">Paper trading journal · auto-generated, no manual refresh needed</div>

  <div class="stat-grid">
    <div class="stat-tile"><div class="label">Decisions logged</div><div class="value">{stats['total']}</div></div>
    <div class="stat-tile"><div class="label">Trades taken</div><div class="value">{stats['traded']}</div></div>
    <div class="stat-tile"><div class="label">Win rate</div><div class="value">{stats['win_rate']:.1f}%</div></div>
    <div class="stat-tile"><div class="label">Total P&amp;L</div><div class="value {pnl_class}">${stats['total_pnl']:,.2f}</div></div>
  </div>

  <section>
    <h2>Equity curve (cumulative paper P&amp;L)</h2>
    {equity_svg}
  </section>

  <section>
    <h2>Per-symbol breakdown</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>Trades</th><th>Win rate</th><th>P&amp;L</th></tr></thead>
        <tbody>{symbol_rows}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Recent decisions (most recent first)</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Symbol</th><th>Trade</th><th>Outcome</th><th>Conf.</th><th>P&amp;L</th><th>Reasoning</th></tr></thead>
        <tbody>{recent_rows}</tbody>
      </table>
    </div>
  </section>
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return out_path


if __name__ == "__main__":
    cfg = load_config()
    path = generate_dashboard(cfg["journal"]["path"])
    print(f"Dashboard written to {path}")
