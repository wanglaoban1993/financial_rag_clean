"""Standalone Streamlit demo — reads only bundled demo_data/, no backend services.

Unlike server_app/dashboard.py (which calls the api/agent-api services backed by
Postgres and live market-data/LLM providers), this app never makes a network
call: it reads the CSVs and sample docs already committed under demo_data/, and
invents the rest (price history, analyst targets, recommendations; see synthetic.py).
That makes it safe to run with `streamlit run demo/dashboard_demo.py` or deploy
to Streamlit Community Cloud without any secrets, API keys, or database.

The click-through dialogs use the modules of the full dashboard that have no I/O:
server_app/history_charts.py (price chart with EMA 20 and MACD), server_app/target_chart.py
(price-target and recommendation cards) and server_app/diff_explain.py (day-change walk-through).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # `streamlit run demo/dashboard_demo.py` only puts demo/ on the path
    sys.path.insert(0, str(ROOT))

from demo import synthetic  # noqa: E402
from server_app import diff_explain, history_charts, target_chart  # noqa: E402

DEMO_DATA_DIR = ROOT / "demo_data"
CSV_UPDATE_DIR = DEMO_DATA_DIR / "csv_update"
SAMPLE_DOCS_DIR = DEMO_DATA_DIR / "sample_docs"

ACCENT, MUTED, POS, NEG = "#1d7fff", "#64748b", "#0f9d58", "#d92d43"
DAY_CHANGE = diff_explain.DAY_CHANGE_COLUMN

st.set_page_config(page_title="Watchlist Intelligence — Demo", layout="wide", page_icon="🛰️")

NUMERIC_COLUMNS = [
    "Bid", "Ask",
    "% Differenz ein Jahr", "% Differenz ein Monat", DAY_CHANGE,
]
# The columns of the full dashboard's equity table (the CSV also carries QUANTITY and comments), with
# the clickable ones first so they stay in view. Fixed widths keep the table inside the page.
DISPLAY_COLUMNS = [
    "WKN", "ISIN", "Name", "Bid", "Ask", "TMP", "Potential %", DAY_CHANGE,
    "% Differenz ein Monat", "% Differenz ein Jahr", "DEV",
]
COLUMN_WIDTHS = {
    "WKN": 72, "ISIN": 110, "Name": 132, "Bid": 58, "Ask": 58, "TMP": 64, "Potential %": 92,
    DAY_CHANGE: 195, "% Differenz ein Monat": 150, "% Differenz ein Jahr": 138, "DEV": 48,
}
HISTORY_CELLS = {"ISIN", "WKN", "NAME", "COMMENT"}


@st.cache_data
def load_watchlist(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, sep=";", decimal=",", engine="python")
    df = df.dropna(axis=1, how="all")
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data
def discover_sectors(asset_class: str) -> dict[str, Path]:
    folder = CSV_UPDATE_DIR / asset_class
    if not folder.is_dir():
        return {}
    return {path.stem: path for path in sorted(folder.glob("*.csv"))}


@st.cache_data
def price_history(isin: str, ask: float) -> pd.DataFrame:
    return synthetic.price_history(isin, ask)


def style_table(df: pd.DataFrame):
    change_cols = [c for c in df.columns if c.startswith("%") or c == "Potential %"]
    formats = {c: "{:+.2f}%" for c in change_cols}
    formats.update({c: "{:.2f}" for c in ("Bid", "Ask", "TMP") if c in df.columns})
    return df.style.map(
        lambda v: "color: #0f9d58" if isinstance(v, (int, float)) and v > 0
        else ("color: #d92d43" if isinstance(v, (int, float)) and v < 0 else ""),
        subset=change_cols,
    ).format(formats)


def render_history(history: pd.DataFrame, key: str) -> None:
    """Price chart with an optional EMA 20 on it and an optional MACD panel below, as in the full dashboard."""
    ema_col, macd_col = st.columns(2)
    show_ema = ema_col.checkbox("EMA 20", key=f"{key}_ema20")
    show_macd = macd_col.checkbox("MACD", key=f"{key}_macd")
    chart = history_charts.build_history_chart(history, accent=ACCENT, muted=MUTED, ema_20=show_ema, macd=show_macd)
    st.vega_lite_chart(history_charts.to_spec(chart), use_container_width=True)


@st.dialog("Price history", width="medium")
def show_history(name: str, isin: str, wkn: str, ask: float) -> None:
    st.markdown(f"**{name}**")
    st.caption(f"ISIN {isin} · WKN {wkn} · invented price history")
    render_history(price_history(isin, ask), key=f"dialog_{isin}")
    st.caption(
        "EMA 20 is drawn on the price; the MACD (12, 26, 9) panel below marks where its line crosses the "
        "signal line (buy / sell). Ctrl + mouse wheel zooms, dragging pans, as in the full dashboard."
    )


@st.dialog("Target price", width="medium")
def show_target(name: str, isin: str, wkn: str, ask: float, tmp: float) -> None:
    st.markdown(f"**{name}**")
    st.caption(f"ISIN {isin} · WKN {wkn} · TMP in the table {tmp:.2f} EUR")
    target = synthetic.analyst_targets(isin, ask)
    svg = target_chart.price_target_svg(target["low"], target["mean"], target["high"], ask, "EUR", "Ask")
    recs = synthetic.recommendations(isin, target["upside"], target["analysts"])
    components.html(target_chart.target_cards_html(svg, recs), height=target_chart.CARDS_HEIGHT)
    st.caption(
        "Invented targets and recommendations. The full system shows the analyst targets Yahoo publishes "
        "(cached twice a day), converted into the row's currency, and the recommendations fetched when "
        "the dialog opens, with links to the source pages."
    )


@st.dialog("Differenz zum Vortageskurs", width="medium")
def show_day_change(name: str, isin: str, wkn: str, raw: dict) -> None:
    st.markdown(f"**{name}**")
    st.caption(f"ISIN {isin} · WKN {wkn}")
    result = diff_explain.explain_day_change(raw)
    if not result.ok:
        st.warning(result.message)
        for note in result.notes:
            st.caption(note)
        return
    for label, value in result.inputs:
        left, right = st.columns([3, 2])
        left.markdown(label)
        right.markdown(f"**{value}**")
    st.markdown("**Berechnung**")
    st.code("\n".join(result.steps), language=None)
    color = POS if result.result_pct >= 0 else NEG
    st.markdown(
        f'<div style="font-size:1.3rem;font-weight:700;color:{color}">'
        f"{diff_explain.fmt(result.result_abs, signed=True)} {result.currency} · "
        f"{diff_explain.fmt(result.result_pct, signed=True)} %</div>",
        unsafe_allow_html=True,
    )
    for note in result.notes:
        st.caption(note)
    st.caption("The demo has no real previous close: it is the one that reproduces the table's percentage.")


def render_table(df: pd.DataFrame, key: str) -> None:
    """The table; a click on a cell opens the dialog of that column, like in the full dashboard."""
    shown = df[[c for c in DISPLAY_COLUMNS if c in df.columns]]
    hint = "ISIN, WKN or name → price history with EMA 20 and MACD"
    if "TMP" in shown.columns:
        hint += " · TMP → target price and analyst recommendations"
    st.caption(f"Click a cell: {hint} · % Differenz zum Vortageskurs → how it is calculated")
    event = st.dataframe(
        style_table(shown), use_container_width=True, hide_index=True,
        column_config={c: st.column_config.Column(width=COLUMN_WIDTHS[c]) for c in shown.columns},
        on_select="rerun", selection_mode="single-cell", key=key,
    )

    # A selection stays in the widget until the user changes it: remember what was opened last so the
    # dialog does not come back on every rerun.
    cells = event.selection.cells if event and event.selection else []
    state_key, identity = f"_opened_{key}", None
    if cells:
        row_position, column = cells[0]
        column = str(column).strip().upper()
        kind = "target" if column == "TMP" else "day" if column == DAY_CHANGE.upper() else "history" if column in HISTORY_CELLS else None
        identity = (kind, row_position) if kind else None
    if identity and st.session_state.get(state_key) != identity:
        st.session_state[state_key] = identity
        kind, row_position = identity
        row = df.iloc[row_position]
        name, isin, wkn, ask = row["Name"], row["ISIN"], str(row["WKN"]), float(row["Ask"])
        if kind == "history":
            show_history(name, isin, wkn, ask)
        elif kind == "target":
            show_target(name, isin, wkn, ask, float(row["TMP"]))
        else:
            show_day_change(name, isin, wkn, synthetic.day_change_input(row))
    elif not identity:
        st.session_state[state_key] = None


st.title("🛰️ Watchlist Intelligence — Public Demo")
st.caption(
    "Sample data only — prices, price history, analyst targets and commentary below are invented "
    "and bundled in this repo under demo_data/. This page makes no external API or database calls."
)

tab_equity, tab_etf, tab_commentary, tab_qa = st.tabs(["Equity", "ETF", "Market Commentary", "Ask the Documents"])

for tab, asset_class in ((tab_equity, "EQUITY"), (tab_etf, "ETF")):
    with tab:
        sectors = discover_sectors(asset_class)
        if not sectors:
            st.info(f"No demo data found for {asset_class}.")
            continue
        sector_tabs = st.tabs(list(sectors.keys()))
        for sector_tab, (sector_name, csv_path) in zip(sector_tabs, sectors.items()):
            with sector_tab:
                df = load_watchlist(csv_path)
                if asset_class == "EQUITY":  # ETFs carry no analyst target, as in the full dashboard
                    df = synthetic.add_target_columns(df)
                gainers = int((df.get(DAY_CHANGE, pd.Series(dtype=float)) > 0).sum())
                losers = int((df.get(DAY_CHANGE, pd.Series(dtype=float)) < 0).sum())
                col1, col2, col3 = st.columns(3)
                col1.metric("Instruments", len(df))
                col2.metric("Up today", gainers)
                col3.metric("Down today", losers)
                render_table(df, key=f"table_{asset_class}_{sector_name}")

                if "Name" in df.columns and "Ask" in df.columns:
                    st.subheader("📈 Price history (sample)")
                    st.caption(
                        "Invented 1-year price path — not real quotes. The full dashboard instead "
                        "charts actual fetched history."
                    )
                    picked_name = st.selectbox(
                        "Instrument", df["Name"], key=f"history_pick_{sector_name}"
                    )
                    picked_row = df.loc[df["Name"] == picked_name].iloc[0]
                    render_history(
                        price_history(picked_row.get("ISIN", picked_name), picked_row["Ask"]),
                        key=f"inline_{asset_class}_{sector_name}",
                    )

with tab_commentary:
    st.caption("Static excerpts bundled with the demo — not a live RAG/LLM answer.")
    for md_path in sorted(SAMPLE_DOCS_DIR.glob("*.md")):
        with st.expander(md_path.stem.replace("_", " ").title()):
            st.markdown(md_path.read_text(encoding="utf-8"))

# Preset Q&A pairs illustrating the real system's document-grounded chat, without making
# any LLM call — the live system instead embeds demo_data/sample_docs with rag/embedder.py,
# retrieves the matching chunks via FAISS, and generates the answer with Ollama/MiniMax.
_SAMPLE_QA = [
    (
        "What's driving demand in AI chips right now?",
        "According to the bundled sample commentary, demand for AI accelerators has stayed "
        "strong, with order backlogs extending into next year and advanced-node foundry "
        "capacity as the main bottleneck. Watch quarterly capex guidance from major cloud "
        "providers for the next signal.\n\n*Source: market_commentary_ai_chips.md*",
    ),
    (
        "How are ETF flows looking this month?",
        "The sample note says broad global equity ETFs kept seeing steady inflows, while "
        "emerging-market ETFs had more mixed, regionally divergent flows. Gold/commodity "
        "products also attracted renewed interest as a hedge.\n\n*Source: market_commentary_etf_flows.md*",
    ),
    (
        "Any notes on the healthcare sector?",
        "Per the sample sector note, healthcare has shown resilience with defensive inflows, "
        "GLP-1 therapies dominating headlines, and large pharma navigating patent cliffs via "
        "pipeline investment and bolt-on acquisitions.\n\n*Source: market_commentary_healthcare.md*",
    ),
]

with tab_qa:
    st.caption(
        "Preset question/answer cards — illustrates the real system's document Q&A without "
        "making any LLM call or requiring API keys. The live system (query.py / rag/retriever.py) "
        "embeds your documents, retrieves the matching chunks with FAISS, and generates the "
        "answer with a local (Ollama) or cloud (MiniMax) LLM."
    )
    picked_question = st.selectbox("Sample question", [q for q, _ in _SAMPLE_QA])
    answer = dict(_SAMPLE_QA)[picked_question]
    with st.chat_message("user"):
        st.write(picked_question)
    with st.chat_message("assistant"):
        st.write(answer)
