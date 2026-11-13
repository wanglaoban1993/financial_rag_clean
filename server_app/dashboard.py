from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# `streamlit run server_app/dashboard.py` puts this folder (not the project root)
# on sys.path, so a plain `server_app.` import only works from the root.
try:
    from server_app import diff_explain, history_charts, target_chart
    from server_app.shared_lists import COPY_TARGETS
except ImportError:
    import diff_explain
    import history_charts
    import target_chart
    from shared_lists import COPY_TARGETS


API_URL = os.getenv("API_URL", "http://api:8000").rstrip("/")
AGENT_API_URL = os.getenv("AGENT_API_URL", "http://agent-api:8100").rstrip("/")
REFRESH_SECONDS = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "60"))
# Per-browser-session cap on Assistant messages (0 = unlimited). Every /chat call
# reaches agent-api from this same dashboard process regardless of which visitor
# triggered it, so agent-api can only enforce a global daily budget — this session
# cap is what keeps one visitor from using up the whole demo's budget alone.
SESSION_MESSAGE_LIMIT = int(os.getenv("DEMO_SESSION_MESSAGE_LIMIT", "0"))

st.set_page_config(page_title="Watchlist Intelligence", layout="wide", page_icon="🛰️")

# ---------------------------------------------------------------------------
# Theme toggle — must run before the color tokens/CSS below are built, since
# the whole script re-executes top to bottom on every interaction (including
# clicking the toggle itself).
# ---------------------------------------------------------------------------
if "theme" not in st.session_state:
    st.session_state.theme = "light"

_, _toggle_col = st.columns([8, 1])
with _toggle_col:
    dark_mode = st.toggle(
        "🌙 Dark mode",
        value=(st.session_state.theme == "dark"),
        key="theme_toggle",
    )
st.session_state.theme = "dark" if dark_mode else "light"

# ---------------------------------------------------------------------------
# Color / typography tokens — light (default) and dark trading-terminal theme
# ---------------------------------------------------------------------------
PALETTES = {
    "light": dict(
        BG="#f5f7fa",
        CONSOLE="#ffffff",
        PANEL="#ffffff",
        BORDER="#dbe2ea",
        POS="#0f9d58",
        POS_DIM="rgba(15,157,88,0.10)",
        NEG="#d92d43",
        NEG_DIM="rgba(217,45,67,0.10)",
        ACCENT="#1d7fff",
        TEXT="#1a2b3c",
        SUB="#66788c",
    ),
    "dark": dict(
        BG="#040d1a",
        CONSOLE="#071726",
        PANEL="#0b1e30",
        BORDER="#0f2035",
        POS="#00d48c",
        POS_DIM="rgba(0,212,140,0.12)",
        NEG="#ff3d55",
        NEG_DIM="rgba(255,61,85,0.12)",
        ACCENT="#1d7fff",
        TEXT="#c6ddf0",
        SUB="#3d6080",
    ),
}
_palette = PALETTES[st.session_state.theme]
BG = _palette["BG"]
CONSOLE = _palette["CONSOLE"]
PANEL = _palette["PANEL"]
BORDER = _palette["BORDER"]
POS = _palette["POS"]
POS_DIM = _palette["POS_DIM"]
NEG = _palette["NEG"]
NEG_DIM = _palette["NEG_DIM"]
ACCENT = _palette["ACCENT"]
TEXT = _palette["TEXT"]
SUB = _palette["SUB"]
MONO = "Arial, Helvetica, sans-serif"
SANS = "Arial, Helvetica, sans-serif"


@st.cache_data(ttl=REFRESH_SECONDS)
def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(f"{API_URL}{path}", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def normalize_dataframe(items: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(items)
    for column in ("return_1y", "return_1m", "return_1d"):
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    for column in ("price_date", "ask_time"):
        if column in df:
            df[column] = pd.to_datetime(df[column], errors="coerce")
    return df


def format_timestamp(value: Any) -> str:
    if not value:
        return "n/a"
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return str(value)
    return timestamp.strftime("%Y-%m-%d %H:%M")


# Approximate glyph widths (px) of the dataframe's 14px sans-serif canvas font,
# measured from rendered tables: digits and capitals are wide, lowercase narrow.
# Averaging them would over-size text columns and clip ISIN-like ones.
_DIGIT_PX, _UPPER_PX, _LOWER_PX, _SPACE_PX, _OTHER_PX = 7.4, 8.4, 5.9, 3.6, 5.2
_BOLD_FACTOR = 1.08
_SAFETY_FACTOR = 1.06  # estimates ran 5-8% under the rendered widths
_CELL_PAD_PX = 22
_MIN_COL_PX, _MAX_COL_PX = 60, 480


def _text_px(text: str) -> float:
    total = 0.0
    for ch in text:
        if ch.isdigit():
            total += _DIGIT_PX
        elif ch.isupper():
            total += _UPPER_PX
        elif ch.islower():
            total += _LOWER_PX
        elif ch == " ":
            total += _SPACE_PX
        else:
            total += _OTHER_PX
    return total


def _autosize_width(values: pd.Series, bold: bool = False) -> int:
    """Pixel width that fits the widest cell of a column (header length is ignored)."""
    texts = values.head(2000).fillna("").astype(str).unique()
    cell_px = max((_text_px(t) for t in texts), default=0.0)
    if bold:
        cell_px *= _BOLD_FACTOR
    px = cell_px * _SAFETY_FACTOR + _CELL_PAD_PX
    return int(min(max(px, _MIN_COL_PX), _MAX_COL_PX))


def parse_de_number(value: Any) -> float | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    normalized = (
        text.replace("%", "")
        .replace("EUR", "")
        .replace("USD", "")
        .replace(".", "")
        .replace(",", ".")
        .strip()
    )
    try:
        number = float(normalized)
    except ValueError:
        return None
    return None if number != number else number  # filters NaN


def filter_dataframe(
    df: pd.DataFrame,
    asset_group: str,
    signal_filter: str,
    performance_filter: str,
    coverage_filter: str,
) -> pd.DataFrame:
    filtered = df.copy()
    if filtered.empty:
        return filtered

    if asset_group != "All" and "asset_group" in filtered:
        filtered = filtered[filtered["asset_group"].fillna("Unknown") == asset_group]

    if signal_filter == "Has signal" and "comment3" in filtered:
        filtered = filtered[filtered["comment3"].fillna("").str.strip() != ""]
    elif signal_filter == "Double buy" and "comment3" in filtered:
        filtered = filtered[filtered["comment3"].fillna("").str.upper().str.contains("BB|BUY", regex=True)]

    if performance_filter == "Positive 1Y" and "return_1y" in filtered:
        filtered = filtered[filtered["return_1y"] > 0]
    elif performance_filter == "Negative 1Y" and "return_1y" in filtered:
        filtered = filtered[filtered["return_1y"] < 0]

    if coverage_filter == "Has TMP" and "tmp" in filtered:
        filtered = filtered[filtered["tmp"].fillna("").str.strip() != ""]
    elif coverage_filter == "Missing TMP" and "tmp" in filtered:
        filtered = filtered[filtered["tmp"].fillna("").str.strip() == ""]

    return filtered


ZEBRA_ROW = "#eceff4"  # odd rows of the data table; even rows keep the white default


def cell_return_style(value: Any) -> str:
    # German-locale display columns hand this a comma-decimal string (e.g.
    # "-3,49") rather than a float — parse it back to compare the sign.
    if isinstance(value, str):
        value = parse_de_number(value)
    if value is None or pd.isna(value):
        return f"color: {SUB}; font-family: {MONO}"
    if value > 0:
        return f"color: {POS}; background-color: {POS_DIM}; font-weight: 700; font-family: {MONO}"
    if value < 0:
        return f"color: {NEG}; background-color: {NEG_DIM}; font-weight: 700; font-family: {MONO}"
    return f"color: {SUB}; font-family: {MONO}"


def cell_signal_style(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.replace("-", "") == "":
        return f"color: {SUB}; font-family: {MONO}"
    if text.startswith("!"):
        return f"color: {POS}; background-color: {POS_DIM}; font-weight: 700; font-family: {MONO}"
    if text.startswith("?"):
        return f"color: {NEG}; background-color: {NEG_DIM}; font-weight: 700; font-family: {MONO}"
    return f"color: {SUB}; font-family: {MONO}"


def stat_card_html(label: str, value: str, sub: str = "", color: str = TEXT) -> str:
    sub_html = f'<div class="stat-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="stat-card">'
        f'<div class="stat-label">{label}</div>'
        f'<div class="stat-value" style="color:{color}">{value}</div>'
        f"{sub_html}"
        f"</div>"
    )


def render_stat_row(cards: list[tuple[str, str, str, str]]) -> None:
    html = "".join(stat_card_html(*card) for card in cards)
    st.markdown(f'<div class="stat-row">{html}</div>', unsafe_allow_html=True)


TICKER_WATCHLIST = "top_liste_latest"
TICKER_SECONDS_PER_ITEM = 2  # keeps the scroll speed the same however many items there are


def render_ticker(df: pd.DataFrame) -> None:
    if df.empty or "return_1d" not in df:
        return
    # return_1d is the "% Differenz zum Vortageskurs" column. The top list repeats
    # an instrument once per index it belongs to, so show each ISIN only once.
    subset = df.dropna(subset=["return_1d"])
    if "isin" in subset:
        subset = subset[~subset["isin"].duplicated() | subset["isin"].isna()]
    if subset.empty:
        return

    items = []
    for _, row in subset.iterrows():
        d1 = row.get("return_1d")
        arrow = "▲" if d1 >= 0 else "▼"
        color = POS if d1 >= 0 else NEG
        bid = row.get("bid_text") or "—"
        symbol = str(row.get("wkn") or row.get("name") or "")[:14]
        items.append(
            '<span class="tk-item">'
            f'<span class="tk-symbol">{symbol}</span>'
            f'<span class="tk-bid">{bid}</span>'
            f'<span class="tk-change" style="color:{color}">{arrow} {abs(d1):.2f}%</span>'
            "</span>"
        )
    strip = "".join(items) * 2
    st.markdown(
        f'<div class="ticker-wrap"><div class="ticker-track" '
        f'style="animation-duration:{max(60, TICKER_SECONDS_PER_ITEM * len(subset))}s">{strip}</div></div>',
        unsafe_allow_html=True,
    )


SOURCE_ICONS = {
    "pdf": "📄", "email": "✉️", "market": "📈", "quote": "💹",
    "watchlist": "⭐", "file": "🗂️", "document": "📄",
}


def render_sources(sources: list[dict[str, Any]]) -> None:
    if not sources:
        return
    with st.expander(f"Sources ({len(sources)})"):
        for source in sources:
            icon = SOURCE_ICONS.get(source.get("kind", ""), "•")
            line = f"{icon} **{source.get('title') or 'unknown'}**"
            if source.get("detail"):
                line += f" — {source['detail']}"
            st.markdown(line)
            if source.get("snippet"):
                st.caption(source["snippet"])


def render_grounding_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    st.caption(
        "⚠️ Not found in the retrieved sources — please verify: "
        + ", ".join(f"`{w}`" for w in warnings)
    )


def stream_chat(
    prompt: str, history: list[dict[str, str]], status: Any
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Call agent-api's /chat/stream, writing each progress line into `status`
    as it arrives; returns (answer, sources, grounding_warnings) from the final event."""
    answer, sources, warnings = "", [], []
    with requests.post(
        f"{AGENT_API_URL}/chat/stream",
        json={"message": prompt, "history": history},
        stream=True,
        # Read timeout applies between chunks, and progress lines keep arriving
        # while the agent works, so this only trips if the agent goes silent.
        timeout=(10, 120),
    ) as response:
        response.raise_for_status()
        response.encoding = "utf-8"
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            if event["type"] == "status":
                status.write(f"• {event['text']}")
            elif event["type"] == "answer":
                answer, sources = event["answer"], event.get("sources") or []
                warnings = event.get("grounding_warnings") or []
            elif event["type"] == "error":
                raise RuntimeError(event["message"])
    if not answer:
        raise RuntimeError("agent returned no answer")
    return answer, sources, warnings


def typewriter(text: str, max_seconds: float = 3.0):
    """Yield `text` in small chunks so st.write_stream types it out. The agent
    only returns the answer once it has passed the grounding check, so this is
    a display effect over the finished answer, not token streaming from the
    model; the total delay is capped so long answers don't drag."""
    chunks = re.findall(r"\S+\s*", text) or [text]
    delay = min(0.02, max_seconds / len(chunks))
    for chunk in chunks:
        yield chunk
        time.sleep(delay)


def render_assistant_chat() -> None:
    st.divider()
    st.subheader("Assistant")
    st.caption("Ask about your watchlists, indexed documents, or live prices.")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    history_box = st.container(height=560)
    with history_box:
        for message in st.session_state.chat_history:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                render_grounding_warnings(message.get("grounding_warnings") or [])
                render_sources(message.get("sources") or [])

    user_message_count = sum(1 for m in st.session_state.chat_history if m["role"] == "user")
    if SESSION_MESSAGE_LIMIT > 0 and user_message_count >= SESSION_MESSAGE_LIMIT:
        st.info(f"Limited to {SESSION_MESSAGE_LIMIT} messages this session. Refresh to start over.")
        return

    prompt = st.chat_input("Ask something...")
    if not prompt:
        return

    st.session_state.chat_history.append({"role": "user", "content": prompt})
    # Full prior transcript except the message just appended above — the
    # agent-api is stateless and relies on the client for history. Only
    # role/content go over the wire; "sources" is display-only.
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.chat_history[:-1]
    ]

    sources: list[dict[str, Any]] = []
    warnings: list[str] = []
    with history_box:
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            status = st.status("Thinking…", expanded=True)
            failed = False
            try:
                answer, sources, warnings = stream_chat(prompt, history, status)
                status.update(label="Done", state="complete", expanded=False)
            except Exception as exc:
                failed = True
                answer = f"Assistant unavailable: {exc}"
                status.update(label="Failed", state="error", expanded=False)
            if failed:
                st.markdown(answer)
            else:
                st.write_stream(typewriter(answer))
            render_grounding_warnings(warnings)
            render_sources(sources)

    st.session_state.chat_history.append({
        "role": "assistant", "content": answer, "sources": sources, "grounding_warnings": warnings,
    })


CSS = f"""
<style>
  html, body, .stApp {{
    background: {BG} !important;
    color: {TEXT} !important;
    font-family: {SANS};
  }}

  section[data-testid="stSidebar"] {{
    background: {CONSOLE} !important;
    border-right: 1px solid {BORDER};
  }}
  section[data-testid="stSidebar"] * {{
    color: {TEXT} !important;
  }}

  .block-container {{
    padding-top: 1rem;
    padding-bottom: 2rem;
  }}

  h1, h2, h3, h4 {{
    color: {TEXT} !important;
    letter-spacing: 0.02em;
  }}

  .ticker-wrap {{
    overflow: hidden;
    background: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    height: 30px;
    display: flex;
    align-items: center;
    margin-bottom: 14px;
  }}
  .ticker-track {{
    display: flex;
    white-space: nowrap;
    animation: ticker 60s linear infinite;
  }}
  .ticker-track:hover {{ animation-play-state: paused; }}
  @keyframes ticker {{
    0% {{ transform: translateX(0); }}
    100% {{ transform: translateX(-50%); }}
  }}
  .tk-item {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 0 16px; border-right: 1px solid {BORDER};
    font-family: {MONO}; font-size: 11px;
  }}
  .tk-symbol {{ color: {SUB}; font-weight: 700; }}
  .tk-bid {{ color: {TEXT}; }}
  .tk-change {{ font-weight: 700; }}

  .app-header {{
    background: linear-gradient(180deg, #071f38 0%, {BG} 100%);
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .app-title {{ font-size: 17px; font-weight: 800; letter-spacing: 0.06em; color: {TEXT}; }}
  .app-subtitle {{
    font-size: 10px; color: {SUB}; letter-spacing: 0.1em; text-transform: uppercase;
    font-family: {MONO}; margin-top: 2px;
  }}
  .app-live {{ color: {ACCENT}; font-weight: 700; font-family: {MONO}; font-size: 11px; text-align: right; }}
  .app-updated {{ color: {SUB}; font-family: {MONO}; font-size: 10px; text-align: right; }}

  .stat-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }}
  .stat-card {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 10px 14px; flex: 1 1 130px; min-width: 120px;
  }}
  .stat-label {{
    color: {SUB}; font-size: 9px; font-weight: 700; letter-spacing: 0.12em;
    text-transform: uppercase; margin-bottom: 4px; font-family: {SANS};
  }}
  .stat-value {{ font-size: 18px; font-family: {MONO}; font-weight: 700; line-height: 1; }}
  .stat-sub {{ color: {SUB}; font-size: 9px; margin-top: 3px; font-family: {SANS}; }}

  button[data-baseweb="tab"] {{
    font-family: {SANS}; font-weight: 700; letter-spacing: 0.06em;
    color: {SUB};
  }}
  button[data-baseweb="tab"][aria-selected="true"] {{ color: {ACCENT} !important; }}

  [data-testid="stDataFrame"] {{ font-family: {MONO}; }}

  [data-testid="stTextInput"] input,
  [data-baseweb="select"] > div,
  [data-baseweb="input"] {{
    background-color: {CONSOLE} !important;
    color: {TEXT} !important;
    border-color: {BORDER} !important;
  }}

  /* config.toml's secondaryBackgroundColor is a light yellow so that the
     table's row-hover color (a 30% blend of it into backgroundColor, drawn
     on a canvas that CSS can't reach) comes out light yellow. Streamlit also
     paints inputs, dropdowns and expander headers with that same color, so
     pin those back to the console color. */
  [data-testid="stMain"] [data-testid="stSelectbox"] [role="group"],
  [data-testid="stMain"] [data-testid="stTextInputRootElement"],
  [data-testid="stMain"] [data-testid="stTextAreaRootElement"],
  [role="dialog"] [data-testid="stSelectbox"] [role="group"],
  [role="dialog"] [data-testid="stTextInputRootElement"],
  [role="dialog"] [data-testid="stTextAreaRootElement"] {{
    background-color: {CONSOLE} !important;
    border-color: {CONSOLE} !important;
  }}
  [data-testid="stDialog"] {{
    background-color: rgba(142, 165, 210, 0.25) !important;
  }}
  /* Price-history dialog: nudge the whole popup (the box that is the direct
     child of stDialog — [role="dialog"] is only its inner content) left of
     center so it covers less of the table. Relative offset (not transform) so
     it doesn't fight the open animation; skipped on narrow screens where it
     should stay centered. */
  @media (min-width: 992px) {{
    [data-testid="stDialog"] > div {{
      position: relative;
      right: 18vw;
    }}
  }}
  [data-testid="stCheckbox"] label > div:not([data-testid]) > div {{
    background-color: #ffffff !important;
  }}
  [data-testid="stMain"] [data-testid="stExpander"] summary,
  [data-testid="stMain"] [data-testid="stExpander"] summary:hover,
  [data-testid="stMain"] [data-testid="stExpander"] summary:focus,
  [data-testid="stMain"] [data-testid="stExpander"] summary:active {{
    background-color: {CONSOLE} !important;
  }}

  [data-baseweb="popover"] li,
  [data-baseweb="menu"] {{
    background-color: {CONSOLE} !important;
    color: {TEXT} !important;
  }}

  [data-testid="stCaptionContainer"] {{ color: {SUB} !important; }}

  .stAlert {{
    background-color: {PANEL} !important;
    color: {TEXT} !important;
    border: 1px solid {BORDER} !important;
  }}

</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# Makes dialogs (the price-history popup) draggable by their title and by the empty
# `.dialog-drag-strip` at their bottom. st.markdown
# can't run scripts, so this goes through a zero-height component iframe whose
# script reaches into the parent page; it watches for dialogs being opened.
DIALOG_DRAG_JS = r"""
(function () {
  var win = window.parent, doc = win.document;
  if (win.__dialogDragInit) return;
  win.__dialogDragInit = true;
  var offset = { x: 0, y: 0 };  // remembered while the page stays open

  function attach(box) {
    var apply = function () { box.style.translate = offset.x + 'px ' + offset.y + 'px'; };
    if (!box.__dragReady) { box.__dragReady = true; apply(); }
    // The title and the empty strip under the last button both grab the dialog. The strip
    // is rendered after the title (once the history has loaded), so look again on each scan.
    box.querySelectorAll('[role="dialog"] > h2, .dialog-drag-strip').forEach(function (header) {
      if (!header.__dragReady) grab(box, header, apply);
    });
  }

  function grab(box, header, apply) {
    header.__dragReady = true;
    header.style.cursor = 'move';
    header.style.userSelect = 'none';
    header.style.touchAction = 'none';

    header.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || e.target.closest('button')) return;
      var rect = box.getBoundingClientRect();
      var startX = e.clientX, startY = e.clientY, base = { x: offset.x, y: offset.y };
      var minDx = -rect.left, maxDx = win.innerWidth - rect.right;
      var minDy = -rect.top, maxDy = win.innerHeight - rect.bottom;
      header.setPointerCapture(e.pointerId);
      var move = function (ev) {
        var dx = Math.min(Math.max(ev.clientX - startX, minDx), maxDx);
        var dy = Math.min(Math.max(ev.clientY - startY, minDy), maxDy);
        offset = { x: base.x + dx, y: base.y + dy };
        apply();
      };
      var up = function (ev) {
        header.releasePointerCapture(ev.pointerId);
        header.removeEventListener('pointermove', move);
        header.removeEventListener('pointerup', up);
        header.removeEventListener('pointercancel', up);
      };
      header.addEventListener('pointermove', move);
      header.addEventListener('pointerup', up);
      header.addEventListener('pointercancel', up);
      e.preventDefault();
    });
  }

  function scan() {
    doc.querySelectorAll('[data-testid="stDialog"] > div').forEach(attach);
  }
  new win.MutationObserver(scan).observe(doc.body, { childList: true, subtree: true });
  scan();
})();
"""
with _toggle_col:
    components.html(f"<script>{DIALOG_DRAG_JS}</script>", height=0)


# Tabs whose lists can be created and edited from the dashboard (CSV-backed);
# BUYLIST/DEPOTS hold a mix of equities and ETFs.
SHARED_GROUPS = {"BUYLIST", "DEPOTS"}
EDITABLE_GROUPS = {"EQUITY", "ETF"} | SHARED_GROUPS


def render_history_charts(hist_df: pd.DataFrame, key: str | None) -> None:
    """Price chart, with optional EMA 20 on it and optional MACD and Stochastic
    panels (with buy/sell markers) stacked below on the same time axis. Each is
    switched on with a checkbox, all off by default."""
    ema20_col, macd_col, stoch_col = st.columns(3)
    show_ema20 = ema20_col.checkbox("EMA 20", key=f"hist_ema20_{key}")
    show_macd = macd_col.checkbox("MACD", key=f"hist_macd_{key}")
    show_stoch = stoch_col.checkbox("Stochastic", key=f"hist_stoch_{key}")

    if show_stoch and not history_charts.has_high_low(hist_df):
        st.info("This price source has no daily high/low, so the Stochastic can't be drawn.")
        show_stoch = False

    chart = history_charts.build_history_chart(
        hist_df, accent=ACCENT, muted=SUB,
        ema_20=show_ema20, macd=show_macd, stochastic=show_stoch,
    )
    st.vega_lite_chart(history_charts.to_spec(chart), use_container_width=True)


@st.dialog("Target price", width="medium")
def show_target_source(
    name: str | None, isin: str | None, wkn: str | None, tmp: str | None, raw: dict[str, Any] | None = None
) -> None:
    raw = raw or {}
    st.markdown(f"**{name or 'Unknown instrument'}**")
    st.caption(f"ISIN {isin or '—'} · WKN {wkn or '—'} · TMP in the table {tmp or '—'} {raw.get('DEV') or ''}".rstrip())

    row_currency = str(raw.get("DEV") or "").upper()
    try:
        data = get_json("/target-source", params={"isin": isin, "wkn": wkn, "to_currency": row_currency or None})
    except Exception as exc:
        st.error(f"Could not load the source: {exc}")
        return

    links = data.get("links") or []
    if not links:
        reason = {
            "no_isin": "This row has no ISIN to look up.",
            "no_symbol": "No ticker mapping found for this ISIN yet.",
        }.get(data.get("error"), "No source page known for this instrument.")
        st.info(reason)
        return

    mean, high, low = data.get("target_mean"), data.get("target_high"), data.get("target_low")
    currency = str(data.get("currency") or "").upper()

    if mean is None:
        st.info(
            f"Yahoo has no analyst target for {data.get('symbol')}. If this is a foreign "
            "listing of the stock, the target is usually only published for the primary listing."
        )
    else:
        # Yahoo states the target in the currency of the primary listing (USD, JPY ...) while
        # the row is quoted in its DEV (mostly EUR). The API converts the targets into DEV, so
        # the row's current price can be drawn on the same axis; without a rate the current
        # price is left out rather than mixed with another currency.
        current, current_source = None, ""
        for column in ("Ask", "Bid", "Last"):
            current = parse_de_number(raw.get(column))
            if current:
                current_source = column  # shown as "Current (Ask)": which quote this price is
                break
        display = data.get("display")
        chart_currency = currency
        if display:
            low, mean, high = display.get("target_low"), display.get("target_mean"), display.get("target_high")
            chart_currency = str(display.get("currency") or row_currency)
            if str(data.get("currency") or "") != chart_currency:
                chart_currency = f"{chart_currency} (converted from {data.get('currency')})"
        elif current and row_currency and str(data.get("currency") or "") != row_currency:
            current = None
            st.caption(
                f"Current price not drawn: this row is quoted in {row_currency}, the target in "
                f"{data.get('currency')}, and no exchange rate was available."
            )
        price_svg = (
            target_chart.price_target_svg(low, mean, high, current or None, chart_currency, current_source)
            if low is not None and high is not None
            else ""
        )
        components.html(target_chart.target_cards_html(price_svg, data.get("recommendations") or []), height=target_chart.CARDS_HEIGHT)

    st.caption(f"Symbol {data.get('symbol')}" + (f" · {currency}" if currency else ""))
    for link in links:
        prefix = "" if link.get("primary") else "Fallback: "
        st.markdown(f"- {prefix}[{link['label']}]({link['url']})")
    if data.get("fetched_at"):
        st.caption(f"Cached target last refreshed {data['fetched_at']}")


@st.dialog("Differenz zum Vortageskurs", width="medium")
def show_day_change_calculation(name: str | None, isin: str | None, wkn: str | None, raw: dict[str, Any]) -> None:
    st.markdown(f"**{name or 'Unbekanntes Instrument'}**")
    st.caption(f"ISIN {isin or '—'} · WKN {wkn or '—'}")

    stored = None
    if isin and not diff_explain.parse_number(raw.get("REF_PREV_CLOSE")):
        try:
            stored = get_json("/prev-close", params={"isin": isin})
        except Exception:
            stored = None  # no stored close: the dialog falls back to deriving it
    result = diff_explain.explain_day_change(raw, stored)
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


@st.dialog("Price history", width="medium")
def show_instrument_history(
    name: str | None,
    isin: str | None,
    wkn: str | None,
    note: str | None,
    watchlist_name: str | None = None,
    asset_group: str | None = None,
    comment: str | None = None,
) -> None:
    st.markdown(f"**{name or 'Unknown instrument'}**")
    st.caption(f"ISIN {isin or '—'} · WKN {wkn or '—'}")

    try:
        data = get_json("/history", params={"isin": isin, "wkn": wkn})
    except Exception as exc:
        st.error(f"Could not load history: {exc}")
        data = None

    if data is not None:
        points = data.get("points") or []
        if not points:
            reason = {
                "no_isin": "This row has no ISIN to look up.",
                "no_symbol_mapping": "No ticker mapping found for this ISIN yet.",
            }.get(data.get("error"), "No historical data available.")
            st.info(reason)
        else:
            hist_df = pd.DataFrame(points)
            hist_df["date"] = pd.to_datetime(hist_df["date"])
            hist_df = hist_df.set_index("date")
            render_history_charts(hist_df, key=isin or wkn or name)
            st.caption(f"Symbol {data.get('symbol')} · last {len(points)} trading days (up to 1y)")

    st.divider()
    note_key = f"note_input_{isin or wkn or name}"
    new_note = st.text_area("Note", value=note or "", height=120, key=note_key)
    if st.button("Save note"):
        if not isin and not wkn:
            st.error("This row has no ISIN or WKN, so the note can't be saved.")
        else:
            try:
                response = requests.post(
                    f"{API_URL}/notes",
                    json={"isin": isin, "wkn": wkn, "note": new_note},
                    timeout=10,
                )
                response.raise_for_status()
                result = response.json()
            except Exception as exc:
                st.error(f"Could not save note: {exc}")
            else:
                if result.get("error"):
                    st.error("This row has no resolvable ISIN, so the note can't be saved.")
                else:
                    get_json.clear()
                    st.rerun()

    if asset_group in EDITABLE_GROUPS:
        st.divider()
        comment_key = f"comment_input_{isin or wkn or name}"
        new_comment = st.text_input("Comment", value=comment or "", key=comment_key)
        if st.button("Save comment"):
            if not isin:
                st.error("This row has no ISIN, so the comment can't be saved.")
            else:
                try:
                    response = requests.post(
                        f"{API_URL}/instruments/comment",
                        json={
                            "watchlist_name": watchlist_name,
                            "asset_group": asset_group,
                            "isin": isin,
                            "comment": new_comment,
                        },
                        timeout=15,
                    )
                    response.raise_for_status()
                    result = response.json()
                except Exception as exc:
                    st.error(f"Could not save comment: {exc}")
                else:
                    if result.get("error"):
                        messages = {
                            "isin_not_found": "Could not find this ISIN in its watchlist CSV.",
                            "ambiguous_isin": "This ISIN appears more than once in the watchlist — edit it by hand.",
                            "watchlist_not_found": "Could not find the CSV file for this watchlist.",
                        }
                        st.error(messages.get(result["error"], result["error"]))
                    else:
                        get_json.clear()
                        st.rerun()

    # Empty strip below the last button: a second drag handle, so the dialog can be moved
    # from its bottom as well as its title (see DIALOG_DRAG_JS).
    st.markdown('<div class="dialog-drag-strip" style="height: 80px"></div>', unsafe_allow_html=True)


def split_tmp_kosten(raw_df: pd.DataFrame) -> pd.DataFrame:
    """BUYLIST/DEPOTS keep one COMMENT2 column: an equity's analyst target (a plain
    number) or an ETF's running costs ("0,20 %", or "-" when unknown). Show them as two
    columns, TMP and Laufende Kosten, at COMMENT2's position; each row fills only the one it is."""
    if "COMMENT2" not in raw_df.columns:
        return raw_df
    value = raw_df["COMMENT2"].fillna("").astype(str).str.strip()
    is_kosten = value.str.endswith("%") | value.eq("-")
    position = raw_df.columns.get_loc("COMMENT2")
    raw_df = raw_df.drop(columns=["COMMENT2"])
    raw_df.insert(position, "TMP", value.mask(is_kosten, ""))
    raw_df.insert(position + 1, "Laufende Kosten", value.where(is_kosten, ""))
    return raw_df


def render_data_table(df: pd.DataFrame, total: int, caption: str, table_key: str) -> None:
    st.caption(caption)
    if df.empty:
        if total == 0:
            st.info("This list is empty — choose “+ Add instrument” in the Action menu above to add the first one.")
        else:
            st.info("No rows match the current filters.")
        return

    # `raw` carries every column from the source CSV, in its original column
    # order — build the table from that instead of a fixed field list, so a
    # sector's table always mirrors its CSV exactly, whatever columns that
    # CSV happens to have (EQUITY/ETF vs. TOP_LIST use entirely different
    # schemas).
    raw_records = df["raw"].tolist() if "raw" in df else []
    raw_df = pd.DataFrame(raw_records, index=df.index)
    raw_df = raw_df.loc[:, [c for c in raw_df.columns if c and not c.startswith("Unnamed")]]

    # TOP_LIST names these "Pot"/"Pot %", EQUITY/ETF name them "Kurspotential"/
    # "Kurspotential %" — unify the label across all three tables. Display-only:
    # the underlying CSV/DB column names are untouched.
    # "Ask Time" is not shown in any table (display-only; still in the CSV/DB).
    raw_df = raw_df.drop(columns=["Ask Time"], errors="ignore")
    raw_df = raw_df.rename(columns={
        "Pot": "Potential (EUR)",
        "Kurspotential": "Potential (EUR)",
        "Pot %": "Potential %",
        "Kurspotential %": "Potential %",
    })

    # Per-asset-group column edits (display-only — the CSV/DB columns are
    # untouched). TOP_LIST has neither COMMENT2/COMMENT3 nor Potential, so it
    # falls through unchanged.
    asset_group_value = df["asset_group"].dropna().iloc[0] if "asset_group" in df and not df["asset_group"].dropna().empty else None
    if asset_group_value == "EQUITY":
        raw_df = raw_df.drop(columns=["COMMENT3"], errors="ignore")
        raw_df = raw_df.rename(columns={"COMMENT2": "TMP"})
    elif asset_group_value == "ETF":
        raw_df = raw_df.drop(columns=["COMMENT3", "Potential (EUR)", "Potential %"], errors="ignore")
        if "COMMENT2" not in raw_df.columns:
            raw_df["COMMENT2"] = ""
        raw_df = raw_df.rename(columns={"COMMENT2": "Laufende Kosten"})
    elif asset_group_value in SHARED_GROUPS:
        # Equities and ETFs side by side: COMMENT2 is the analyst target for an
        # equity and the running costs for an ETF, and the Potential columns are
        # simply empty on ETF rows.
        raw_df = raw_df.drop(columns=["COMMENT3"], errors="ignore")
        raw_df = split_tmp_kosten(raw_df)

    table_df = pd.concat([df[["watchlist_name"]], raw_df], axis=1)
    table_df["Note"] = df["note"].fillna("") if "note" in df else ""

    # Best-effort: any column that looks like a percent/change/return field
    # gets parsed to a number and color-coded the same way the rest of the
    # dashboard does; everything else is shown exactly as the CSV has it.
    percent_like = [
        c for c in raw_df.columns
        if any(token in c for token in ("%", "Differenz", "Change", "Pot"))
    ]
    numeric_cols: list[str] = []
    for col in percent_like:
        parsed = table_df[col].apply(parse_de_number)
        if parsed.notna().any():
            # Streamlit's NumberColumn has no locale option for a comma decimal
            # separator, so these are rendered as pre-formatted text (German
            # convention) instead of a numeric column — sorting on these columns
            # is therefore lexicographic, not by value.
            table_df[col] = parsed.apply(lambda v: "" if pd.isna(v) else f"{v:.2f}".replace(".", ","))
            numeric_cols.append(col)

    signal_cols = [c for c in raw_df.columns if c.upper() == "COMMENT3"]

    # Zebra stripes: every second row gets a light grey background. Applied first so the
    # green/red cell styles below still win on the cells they colour. The canvas table body
    # stays in the light config.toml palette in both themes, so this is a fixed colour.
    styled_df = table_df.style.apply(
        lambda frame: pd.DataFrame(
            [[f"background-color: {ZEBRA_ROW}" if i % 2 else ""] * frame.shape[1] for i in range(len(frame))],
            index=frame.index,
            columns=frame.columns,
        ),
        axis=None,
    )
    if numeric_cols:
        styled_df = styled_df.map(cell_return_style, subset=numeric_cols)
    if signal_cols:
        styled_df = styled_df.map(cell_signal_style, subset=signal_cols)
    if asset_group_value in EDITABLE_GROUPS and "COMMENT" in table_df.columns:
        styled_df = styled_df.map(lambda _: "font-weight: bold", subset=["COMMENT"])

    # st.dataframe has no "autosize by default" switch (only the per-column
    # menu entry), so measure each column here and pass pixel widths instead.
    labels = {"watchlist_name": "Watchlist"}
    column_config = {
        col: st.column_config.Column(
            labels.get(col, col),
            width=_autosize_width(
                table_df[col],
                bold=col in numeric_cols or (col == "COMMENT" and asset_group_value in EDITABLE_GROUPS),
            ),
        )
        for col in table_df.columns
    }

    # Fixed display order per asset group, requested to match a specific
    # reading order rather than the source CSV's column order.
    column_order = None
    if asset_group_value == "TOP_LIST":
        desired_order = [
            "watchlist_name", "Index", "Rank", "Name", "ISIN", "Bid", "Bid Time",
            "% Differenz ein Jahr",
            "% Differenz ein Monat", "% Differenz zum Vortageskurs", "Ask", "TMP",
            "Potential (EUR)", "Potential %", "DEV", "Note",
        ]
        column_order = [c for c in desired_order if c in table_df.columns]
    elif asset_group_value == "INDEX":
        desired_order = [
            "watchlist_name", "Index", "Rank", "Name", "ISIN", "Bid", "Bid Time",
            "% Differenz ein Jahr",
            "% Differenz ein Monat", "% Differenz zum Vortageskurs", "Ask", "TMP",
            "Potential (EUR)", "Potential %", "DEV", "Note",
        ]
        column_order = [c for c in desired_order if c in table_df.columns]
    elif asset_group_value == "EQUITY":
        desired_order = [
            "watchlist_name", "WKN", "STOCK_ID", "COMMENT", "ISIN",
            "Name", "Bid", "Bid Time", "% Differenz ein Jahr",
            "% Differenz ein Monat", "% Differenz zum Vortageskurs", "Ask",
            "TMP", "Potential (EUR)", "Potential %", "DEV", "Note",
        ]
        column_order = [c for c in desired_order if c in table_df.columns]
    elif asset_group_value == "ETF":
        desired_order = [
            "watchlist_name", "WKN", "STOCK_ID", "COMMENT",
            "Laufende Kosten", "ISIN", "Name", "Bid", "Bid Time",
            "% Differenz ein Jahr", "% Differenz ein Monat",
            "% Differenz zum Vortageskurs", "Ask", "DEV", "Note",
        ]
        column_order = [c for c in desired_order if c in table_df.columns]
    elif asset_group_value in SHARED_GROUPS:
        desired_order = [
            "watchlist_name", "WKN", "STOCK_ID", "COMMENT", "ISIN",
            "Name", "Bid", "Bid Time", "% Differenz ein Jahr",
            "% Differenz ein Monat", "% Differenz zum Vortageskurs", "Ask",
            "TMP", "Laufende Kosten", "Potential (EUR)", "Potential %", "DEV", "Note",
        ]
        column_order = [c for c in desired_order if c in table_df.columns]

    # st.dataframe (not a plain HTML table) so column drag-to-reorder and
    # click-to-sort keep working. Trade-off: it draws onto a canvas whose
    # colors come from Streamlit's static config.toml theme at container
    # start, so the table body stays in that fixed palette regardless of the
    # light/dark toggle above.
    event = st.dataframe(
        styled_df,
        use_container_width=True,
        hide_index=True,
        height=640,
        column_config=column_config,
        column_order=column_order,
        on_select="rerun",
        selection_mode="single-cell",
        key=f"table_{table_key}",
    )

    # Streamlit reruns this fragment every REFRESH_SECONDS regardless of user
    # action, and a selection stays in the widget state until the user changes
    # it — track what we last opened so the dialog doesn't keep popping back
    # up on every auto-refresh, and only react when the clicked cell is
    # actually the ISIN/WKN column (clicking elsewhere in the row is a no-op).
    selected_cells = event.selection.cells if event and event.selection else []
    state_key = f"_chart_selection_{table_key}"
    identity = None
    if selected_cells:
        row_position, column_name = selected_cells[0]
        column_key = str(column_name).strip().upper()
        if column_key in {"ISIN", "WKN", "NOTE", "COMMENT", "TMP", diff_explain.DAY_CHANGE_COLUMN.upper()}:
            row = df.iloc[row_position]
            kind = {"TMP": "target", diff_explain.DAY_CHANGE_COLUMN.upper(): "day_change"}.get(column_key, "history")
            identity = (kind, row.get("isin"), row.get("wkn"), row.get("name"))

    if identity and st.session_state.get(state_key) != identity:
        st.session_state[state_key] = identity
        if identity[0] == "day_change":
            raw_row = df.iloc[row_position].get("raw")
            show_day_change_calculation(
                row.get("name"), row.get("isin"), row.get("wkn"), raw_row if isinstance(raw_row, dict) else {}
            )
            return
        if identity[0] == "target":
            raw_row = df.iloc[row_position].get("raw")
            show_target_source(
                row.get("name"),
                row.get("isin"),
                row.get("wkn"),
                str(table_df["TMP"].iloc[row_position]) if "TMP" in table_df.columns else None,
                raw_row if isinstance(raw_row, dict) else {},
            )
            return
        show_instrument_history(
            row.get("name"),
            row.get("isin"),
            row.get("wkn"),
            row.get("note"),
            watchlist_name=row.get("watchlist_name"),
            asset_group=row.get("asset_group"),
            comment=row.get("comment"),
        )
    elif not identity:
        st.session_state[state_key] = None


def render_create_watchlist(group_key: str, selected_sector: str | None = None) -> None:
    with st.container():
        # A form (rather than loose widgets) so the name box clears after a
        # successful submit and Enter submits it.
        with st.form(f"new_list_form_{group_key}", clear_on_submit=True, border=False):
            new_name = st.text_input(
                "List name",
                key=f"new_list_name_{group_key}",
                max_chars=60,
                placeholder="e.g. My Watchlist",
            )
            submitted = st.form_submit_button("Create list")

        if not submitted:
            return
        if not new_name.strip():
            st.error("Please enter a list name.")
            return
        try:
            response = requests.post(
                f"{API_URL}/watchlists",
                json={"asset_group": group_key, "name": new_name},
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            st.error(f"Could not create list: {exc}")
            return

        if result.get("error"):
            messages = {
                "missing_name": "Please enter a list name.",
                "name_too_long": "List name is too long (max 60 characters).",
                "invalid_name": (
                    "List name can't contain / \\ ; : * ? \" < > |, or start with _ or . "
                    "or end with _top / _merged."
                ),
                "watchlist_already_exists": f"A list named “{new_name.strip()}” already exists.",
                "watchlist_not_found": f"The {group_key} folder was not found on the server.",
                "unsupported_asset_group": "This tab does not support custom lists.",
            }
            st.error(messages.get(result["error"], result["error"]))
            return

        # The Sector selectbox is already instantiated in this run, so it can't be set here;
        # render_group_tab applies this before the selectbox on the next run.
        st.session_state[f"_pending_sector_{group_key}"] = result["watchlist_name"]
        get_json.clear()
        st.rerun()


def render_delete_watchlist(group_key: str, selected_sector: str) -> None:
    with st.container():
        st.caption(
            f"Deletes the whole list “{selected_sector}” from {group_key}, with all its instruments. "
            "Its file is moved to csv_update/_deleted/ on the server, not erased."
        )
        confirmed = st.checkbox(
            f"Yes, delete “{selected_sector}”", key=f"delete_confirm_{group_key}_{selected_sector}"
        )
        if not st.button("Delete list", key=f"delete_btn_{group_key}", disabled=not confirmed):
            return
        try:
            response = requests.delete(
                f"{API_URL}/watchlists",
                params={"asset_group": group_key, "name": selected_sector},
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            st.error(f"Could not delete {selected_sector}: {exc}")
            return

        if result.get("error"):
            messages = {
                "watchlist_not_found": f"Could not find the file for {selected_sector} (already deleted?).",
                "unsupported_asset_group": "This tab does not support deleting lists.",
            }
            st.error(messages.get(result["error"], result["error"]))
            return

        # The Sector selectbox above has already been instantiated in this run;
        # once the deleted name is gone from its options it falls back to the first list.
        get_json.clear()
        st.rerun()


def render_add_instrument(group_key: str, selected_sector: str) -> None:
    with st.container():
        cols = st.columns([2, 1, 2])
        new_isin = cols[0].text_input("ISIN", key=f"new_isin_{group_key}").strip().upper()
        new_wkn = cols[1].text_input("WKN (optional)", key=f"new_wkn_{group_key}")
        new_comment = cols[2].text_input("Comment (optional)", key=f"new_comment_{group_key}")

        if st.button("Add & auto-fill from onvista", key=f"add_btn_{group_key}"):
            if not new_isin:
                st.error("ISIN is required.")
            else:
                with st.spinner(f"Looking up {new_isin} on onvista.de…"):
                    try:
                        response = requests.post(
                            f"{API_URL}/instruments",
                            json={
                                "watchlist_name": selected_sector,
                                "asset_group": group_key,
                                "isin": new_isin,
                                "wkn": new_wkn,
                                "comment": new_comment,
                            },
                            timeout=20,
                        )
                        response.raise_for_status()
                        result = response.json()
                    except Exception as exc:
                        st.error(f"Could not add {new_isin}: {exc}")
                        result = None

                if result is not None:
                    if result.get("error"):
                        messages = {
                            "isin_already_present": f"{new_isin} is already in {selected_sector}.",
                            "watchlist_not_found": f"Could not find the CSV file for {selected_sector}.",
                            "missing_isin": "ISIN is required.",
                            "unsupported_asset_group": "This tab does not support adding instruments.",
                        }
                        st.error(messages.get(result["error"], result["error"]))
                    elif result.get("lookup_error"):
                        st.warning(
                            f"Added {new_isin}, but the onvista lookup failed ({result['lookup_error']}) — "
                            "Name/Bid/Ask will fill in on the next scheduled refresh instead."
                        )
                        get_json.clear()
                        st.rerun()
                    else:
                        st.success(f"Added {new_isin} — {result.get('name') or 'name pending'}.")
                        get_json.clear()
                        st.rerun()


def render_remove_instrument(group_key: str, selected_sector: str) -> None:
    with st.container():
        rows = get_json("/rows", params={"watchlist": selected_sector, "asset_group": group_key, "limit": 1000})["items"]
        choices = {
            f"{row['isin']} — {row.get('name') or 'unnamed'}": row["isin"]
            for row in rows
            if row.get("isin")
        }
        if not choices:
            st.caption("No ISINs to remove in this watchlist.")
            return

        label = st.selectbox("Instrument", sorted(choices), key=f"remove_select_{group_key}")
        if st.button("Remove", key=f"remove_btn_{group_key}"):
            target_isin = choices[label]
            try:
                response = requests.delete(
                    f"{API_URL}/instruments",
                    params={"watchlist_name": selected_sector, "asset_group": group_key, "isin": target_isin},
                    timeout=15,
                )
                response.raise_for_status()
                result = response.json()
            except Exception as exc:
                st.error(f"Could not remove {target_isin}: {exc}")
                result = None

            if result is not None:
                if result.get("error"):
                    messages = {
                        "isin_not_found": f"{target_isin} was not found in {selected_sector} (already removed?).",
                        "ambiguous_isin": f"{target_isin} appears more than once in {selected_sector} — remove it by hand.",
                        "watchlist_not_found": f"Could not find the CSV file for {selected_sector}.",
                        "unsupported_asset_group": "This tab does not support removing instruments.",
                    }
                    st.error(messages.get(result["error"], result["error"]))
                else:
                    st.success(f"Removed {target_isin} from {selected_sector}.")
                    get_json.clear()
                    st.rerun()


def render_copy_instrument(group_key: str, selected_sector: str) -> None:
    with st.container():
        rows = get_json("/rows", params={"watchlist": selected_sector, "asset_group": group_key, "limit": 1000})["items"]
        choices = {
            f"{row['isin']} — {row.get('name') or 'unnamed'}": row["isin"]
            for row in rows
            if row.get("isin")
        }
        if not choices:
            st.caption("No ISINs to copy in this watchlist.")
            return

        allowed = COPY_TARGETS[group_key]
        targets = {
            f"{item['asset_group']} · {item['watchlist_name']}": item
            for item in sorted(
                get_json("/watchlists"),
                key=lambda w: (allowed.index(w["asset_group"]) if w.get("asset_group") in allowed else 99, w["watchlist_name"]),
            )
            if item.get("asset_group") in allowed
            and not (item["asset_group"] == group_key and item["watchlist_name"] == selected_sector)
        }
        if not targets:
            st.caption("No other list to copy to.")
            return

        label = st.selectbox("Instrument", sorted(choices), key=f"copy_select_{group_key}")
        target_label = st.selectbox("To list", list(targets), key=f"copy_target_{group_key}")
        if st.button("Copy", key=f"copy_btn_{group_key}"):
            isin, target = choices[label], targets[target_label]
            try:
                response = requests.post(
                    f"{API_URL}/instruments/copy",
                    json={
                        "source_group": group_key,
                        "source_watchlist": selected_sector,
                        "target_group": target["asset_group"],
                        "target_watchlist": target["watchlist_name"],
                        "isin": isin,
                    },
                    timeout=30,
                )
                response.raise_for_status()
                result = response.json()
            except Exception as exc:
                st.error(f"Could not copy {isin}: {exc}")
                result = None

            if result is not None:
                if result.get("error"):
                    messages = {
                        "isin_not_found": f"{isin} was not found in {selected_sector} (already removed?).",
                        "ambiguous_isin": f"{isin} appears more than once in {selected_sector} — fix it by hand.",
                        "isin_already_present": f"{isin} is already in {target_label}.",
                        "watchlist_not_found": f"Could not find the CSV file for {selected_sector}.",
                        "target_watchlist_not_found": f"Could not find the CSV file for {target_label}.",
                        "copy_not_allowed": "That list can't take this instrument.",
                    }
                    st.error(messages.get(result["error"], result["error"]))
                else:
                    st.success(f"Copied {isin} to {target_label}.")
                    get_json.clear()
                    st.rerun()


def render_actions(group_key: str, selected_sector: str, actions: dict[str, Any]) -> None:
    """One 'Action' dropdown (same look as the Sector selector); the chosen action's form shows below it."""
    choice = st.selectbox(
        "Action",
        list(actions),
        index=None,
        placeholder="Choose an action…",
        key=f"action_{group_key}",
    )
    if choice:
        actions[choice](group_key, selected_sector)


def render_group_tab(
    group_key: str,
    group_watchlists: list[dict[str, Any]],
    row_limit: int,
    search_query: str,
    signal_filter: str,
    performance_filter: str,
    coverage_filter: str,
) -> None:
    pending = st.session_state.pop(f"_pending_sector_{group_key}", None)
    if pending:
        st.session_state[f"sector_{group_key}"] = pending

    if not group_watchlists:
        if group_key in EDITABLE_GROUPS:
            render_actions(group_key, "", {"+ New list": render_create_watchlist})
        st.info(f"No {group_key} watchlists imported yet.")
        return

    sector_names = sorted(item["watchlist_name"] for item in group_watchlists)
    if len(sector_names) > 1:
        selector_label = "Index" if group_key == "INDEX" else "Sector"
        selected_sector = st.selectbox(selector_label, sector_names, key=f"sector_{group_key}")
    else:
        selected_sector = sector_names[0]
        st.caption(f"Table: {selected_sector}")

    if group_key in EDITABLE_GROUPS:
        render_actions(
            group_key,
            selected_sector,
            {
                "+ New list": render_create_watchlist,
                "+ Add instrument": render_add_instrument,
                "- Remove instrument": render_remove_instrument,
                "⧉ Copy instrument": render_copy_instrument,
                "🗑 Delete this list": render_delete_watchlist,
            },
        )
    elif group_key in COPY_TARGETS:
        # TOP_LIST is rewritten by the scraper, so it can't be edited, but its stocks can be copied out.
        render_actions(group_key, selected_sector, {"⧉ Copy instrument": render_copy_instrument})

    params: dict[str, Any] = {"watchlist": selected_sector, "asset_group": group_key, "limit": row_limit}
    if search_query.strip():
        params["query"] = search_query.strip()

    data = get_json("/rows", params=params)
    df = normalize_dataframe(data["items"])
    filtered_df = filter_dataframe(df, "All", signal_filter, performance_filter, coverage_filter)
    render_data_table(
        filtered_df,
        data["total"],
        caption=(
            f"Showing {len(filtered_df):,} of {data['total']:,} matching rows in {selected_sector} "
            "· click an ISIN/WKN/Note cell to see its 1y price chart and edit its note"
        ),
        table_key=f"{group_key}_{selected_sector}",
    )


GROUP_ORDER = ["TOP_LIST", "EQUITY", "ETF", "INDEX", "BUYLIST", "DEPOTS"]


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def render_dashboard() -> None:
    try:
        health = get_json("/health")
        summary = get_json("/summary")
        watchlists = get_json("/watchlists")
    except Exception as exc:
        st.error(f"API is not reachable: {exc}")
        st.stop()

    try:
        ticker_df = normalize_dataframe(get_json(
            "/rows", params={"watchlist": TICKER_WATCHLIST, "asset_group": "TOP_LIST", "limit": 1000}
        )["items"])
    except Exception:
        ticker_df = pd.DataFrame()  # the ticker is decoration; never block the page on it
    render_ticker(ticker_df)

    st.markdown(
        f"""
        <div class="app-header">
          <div>
            <div class="app-title">🛰️ WATCHLIST INTELLIGENCE</div>
          <div class="app-subtitle">Equity · ETF · Top/Flop · Index · Buylist · Depots</div>
          </div>
          <div>
            <div class="app-live">◉ LIVE DATA</div>
            <div class="app-updated">
              API {health.get("status", "unknown")} · DB {health.get("database", "unknown")} ·
              Import {format_timestamp(summary.get("last_imported_at"))} ·
              Refreshed {datetime.now().strftime("%H:%M:%S")} (every {REFRESH_SECONDS}s)
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    render_stat_row([
        ("Rows", f"{summary.get('rows') or 0:,}", "", TEXT),
        ("Watchlists", f"{summary.get('watchlists') or 0:,}", "", TEXT),
        ("Instruments", f"{summary.get('instruments') or 0:,}", "", TEXT),
    ])

    with st.container(border=True):
        search_col, rows_col, signal_col, perf_col, cov_col = st.columns([3, 1, 1.4, 1.4, 2])
        search_query = search_col.text_input("Search", placeholder="Name, ISIN, WKN, COMMENT")
        row_limit = rows_col.selectbox("Rows", [100, 200, 500, 1000], index=2)
        signal_filter = signal_col.selectbox("Signal", ["All", "Has signal", "Double buy"])
        performance_filter = perf_col.selectbox("Performance", ["All", "Positive 1Y", "Negative 1Y"])
        coverage_filter = cov_col.selectbox("Coverage", ["All", "Has TMP", "Missing TMP"])

    overview_params: dict[str, Any] = {"limit": 300}
    if search_query.strip():
        overview_params["query"] = search_query.strip()
    overview_df = normalize_dataframe(get_json("/rows", params=overview_params)["items"])

    groups: dict[str, list[dict[str, Any]]] = {key: [] for key in GROUP_ORDER}
    for item in watchlists:
        group = item.get("asset_group")
        if group in groups:
            groups[group].append(item)

    tabs = st.tabs(GROUP_ORDER)
    for tab, group_key in zip(tabs, GROUP_ORDER):
        with tab:
            render_group_tab(
                group_key,
                groups[group_key],
                row_limit,
                search_query,
                signal_filter,
                performance_filter,
                coverage_filter,
            )

    # Inside a container so the chat input renders inline under the tables
    # instead of being pinned to the bottom of the viewport.
    with st.container():
        render_assistant_chat()


render_dashboard()
