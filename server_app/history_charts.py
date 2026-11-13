"""The price-history dialog's chart: price (+ optional EMA 20), then optional MACD
and Stochastic panels stacked below it on one shared time axis, like a trading
terminal. One Altair chart, so hovering shows a crosshair across every panel and
zooming/panning moves all panels together."""

from __future__ import annotations

import altair as alt
import pandas as pd

try:
    from server_app import indicators
except ImportError:  # `streamlit run server_app/dashboard.py` puts this folder on sys.path
    import indicators

PRICE_HEIGHT, PANEL_HEIGHT = 150, 150
BUY_COLOR, SELL_COLOR = "#0f9d58", "#d92d43"
EMA_COLOR = "#f59e0b"
HISTOGRAM_COLOR = "#8b8fe8"
ZOOM_VIEW = "zoom_view"
HOVER_POINT_SIZE = 300  # invisible points the tooltip hangs on: ~10 px grab radius around each line


def _x(show_axis: bool) -> alt.X:
    return alt.X("date:T", title=None, axis=alt.Axis(format="%d %b %y", labels=show_axis, ticks=show_axis))


def _long(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return frame.reset_index().melt(id_vars="date", value_vars=columns, var_name="series", value_name="value").dropna()


def _lines(
    frame: pd.DataFrame, colors: dict[str, str], x: alt.X, y: alt.Y, zoom: alt.Parameter | None = None
) -> alt.Chart:
    lines = alt.Chart(_long(frame, list(colors)), name=ZOOM_VIEW if zoom is not None else alt.Undefined).mark_line(
        strokeWidth=1.6, clip=True, tooltip=None
    ).encode(
        x=x,
        y=y,
        color=alt.Color(
            "series:N",
            title=None,
            scale=alt.Scale(domain=list(colors), range=list(colors.values())),
            legend=alt.Legend(orient="top", direction="horizontal", padding=4),
        ),
    )
    return lines.add_params(zoom) if zoom is not None else lines


def _hover_tooltip(
    frame: pd.DataFrame, follow: str, shown: list[str], title: str, scale: alt.Scale | None = None,
    hover: alt.Parameter | None = None,
) -> alt.Chart:
    """Invisible points on the `follow` line, one per day, carrying a tooltip with the
    date and the `shown` values — so hovering a line shows its price."""
    wide = frame.reset_index()[["date", *shown]].dropna(subset=[follow])
    wide[shown] = wide[shown].round(3).astype(object).where(wide[shown].notna(), None)
    tips = [alt.Tooltip("date:T", title="Date", format="%d %b %Y")]
    tips += [alt.Tooltip(field=name, type="quantitative", title=name) for name in shown]
    points = alt.Chart(wide).mark_point(opacity=0, size=HOVER_POINT_SIZE, clip=True).encode(
        x="date:T", y=alt.Y(field=follow, type="quantitative", title=title, scale=scale or alt.Undefined), tooltip=tips
    )
    return points.add_params(hover) if hover is not None else points



def _signal_marks(signals: pd.DataFrame, x: alt.X, title: str, scale: alt.Scale | None = None) -> alt.LayerChart:
    """Buy = green triangle up, sell = red triangle down. Fixed mark colours rather than a
    colour encoding, so they don't join the panel's line legend."""
    def marks(kind: str, shape: str, color: str) -> alt.Chart:
        return alt.Chart(signals[signals["kind"] == kind]).mark_point(
            filled=True, size=60, opacity=1, shape=shape, color=color, clip=True
        ).encode(
            x=x,
            y=alt.Y("value:Q", title=title, scale=scale or alt.Undefined),
            tooltip=[alt.Tooltip("date:T", format="%d %b %Y"), alt.Tooltip("value:Q", title=f"{kind} signal", format=".2f")],
        )

    return alt.layer(marks("buy", "triangle-up", BUY_COLOR), marks("sell", "triangle-down", SELL_COLOR))


def build_history_chart(
    hist_df: pd.DataFrame,
    *,
    accent: str,
    muted: str,
    ema_20: bool = False,
    macd: bool = False,
    stochastic: bool = False,
    width: int | str = "container",
) -> alt.VConcatChart:
    """`hist_df` is indexed by date with close (and high/low for the Stochastic).
    `stochastic` needs high/low — check `has_high_low` first."""
    close = hist_df["close"]
    last_panel = "stochastic" if stochastic else "macd" if macd else "price"

    # Crosshair shared by every panel: it snaps to the nearest day of the price line.
    hover = alt.selection_point(name="hover", fields=["date"], nearest=True, on="pointerover", clear="pointerout", empty=False)
    dates = hist_df.reset_index()[["date"]]

    def crosshair() -> alt.Chart:
        return alt.Chart(dates).mark_rule(color=muted, strokeWidth=1, tooltip=None).encode(
            x="date:T", opacity=alt.condition(hover, alt.value(0.7), alt.value(0))
        )

    # --- price + EMA --------------------------------------------------------
    price_series = {"Close": accent}
    price_frame = pd.DataFrame({"Close": close})
    if ema_20:
        price_frame["EMA 20"] = indicators.ema(close, 20)
        price_series["EMA 20"] = EMA_COLOR
    x = _x(last_panel == "price")
    scale = alt.Scale(zero=False)
    # Ctrl + mouse wheel zooms and dragging pans the time axis, shared by all panels. A plain
    # wheel is left alone (the `!` only swallows the event when the filter passes) so it
    # keeps scrolling the dialog while the pointer is over the chart.
    # Added by hand rather than with `.interactive()`, and each restricted to its views in
    # `to_spec` — Altair otherwise hands the parameter to every view, and Vega rejects
    # the duplicate signals.
    zoom = alt.selection_interval(name="zoom", bind="scales", encodings=["x"], zoom="wheel![event.ctrlKey]")
    price = alt.layer(
        _lines(price_frame, price_series, x, alt.Y("value:Q", title="Price", scale=scale), zoom),
        _hover_tooltip(price_frame, "Close", list(price_series), "Price", scale, hover),
        crosshair(),
    ).properties(height=PRICE_HEIGHT, width=width)
    panels = [price]

    # --- MACD ---------------------------------------------------------------
    if macd:
        frame = indicators.macd(close)
        x = _x(last_panel == "macd")
        histogram = alt.Chart(frame.reset_index()[["date", "Histogram"]].dropna()).mark_bar(
            opacity=0.5, color=HISTOGRAM_COLOR, clip=True, tooltip=None
        ).encode(x=x, y=alt.Y("Histogram:Q", title="MACD (12, 26, 9)"))
        panels.append(alt.layer(
            histogram,
            _lines(frame, {"MACD": accent, "Signal": "#d92d43"}, x, alt.Y("value:Q", title="MACD (12, 26, 9)"), zoom),
            _hover_tooltip(frame, "MACD", ["MACD", "Signal", "Histogram"], "MACD (12, 26, 9)"),
            _signal_marks(indicators.crossovers(frame["MACD"], frame["Signal"]), x, "MACD (12, 26, 9)"),
            crosshair(),
        ).properties(height=PANEL_HEIGHT, width=width))

    # --- Stochastic ---------------------------------------------------------
    if stochastic:
        frame = indicators.stochastic(close, hist_df["high"], hist_df["low"])
        x = _x(last_panel == "stochastic")
        scale = alt.Scale(domain=[0, 100])
        bounds = alt.Chart(pd.DataFrame({"level": [20, 80]})).mark_rule(strokeDash=[4, 4], color=muted, opacity=0.6, tooltip=None).encode(
            y=alt.Y("level:Q", title="Stochastic (14, 3, 3)", scale=scale)
        )
        panels.append(alt.layer(
            bounds,
            _lines(frame, {"%K": accent, "%D": EMA_COLOR}, x, alt.Y("value:Q", title="Stochastic (14, 3, 3)", scale=scale), zoom),
            _hover_tooltip(frame, "%K", ["%K", "%D"], "Stochastic (14, 3, 3)", scale),
            _signal_marks(indicators.crossovers(frame["%K"], frame["%D"]), x, "Stochastic (14, 3, 3)", scale),
            crosshair(),
        ).properties(height=PANEL_HEIGHT, width=width))

    return alt.vconcat(*panels, spacing=8).resolve_scale(x="shared", color="independent")


def has_high_low(hist_df: pd.DataFrame) -> bool:
    return all(col in hist_df and hist_df[col].notna().all() for col in ("high", "low"))


def _view_names(node) -> list[str]:
    if isinstance(node, dict):
        own = [node["name"]] if isinstance(node.get("name"), str) and "mark" in node else []
        return own + [name for key, value in node.items() if key != "datasets" for name in _view_names(value)]
    if isinstance(node, list):
        return [name for value in node for name in _view_names(value)]
    return []


def to_spec(chart: alt.VConcatChart) -> dict:
    """Vega-Lite spec of `build_history_chart`'s chart, for st.vega_lite_chart. Altair gives
    every parameter in a layer to every named view of it (and renames `zoom_view` when
    there are several panels); keep the zoom on the zoom view only and the crosshair off it."""
    spec = chart.to_dict()
    zoom_views = [name for name in _view_names(spec) if name.startswith(ZOOM_VIEW)]
    for param in spec.get("params", []):
        if param["name"] == "zoom":
            param["views"] = zoom_views
        elif param["name"] == "hover":
            param["views"] = [view for view in param.get("views", []) if view not in zoom_views]
    return spec
