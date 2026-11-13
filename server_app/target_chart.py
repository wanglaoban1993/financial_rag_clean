"""Inline-SVG "Analyst Price Targets" bar, modelled on Yahoo Finance's analysis page.

A horizontal axis from the lowest to the highest analyst target, a marker with the
average above it and, when the price is known, a marker with the current price below.
Pure function so it can be tested without Streamlit; the dashboard shows the result
with st.html.
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional, Sequence

WIDTH = 420
HEIGHT = 162
MARGIN_X = 52  # room for the end labels and the half width of a marker box
BAR_Y = 70
BOX_W = 84
BOX_H = 38

# Height components.html needs for the two stacked cards (two charts at max-width 480px).
CARDS_HEIGHT = 490

TEXT = "#1a2b3c"
MUTED = "#64748b"
BAR = "#cbd5e1"
ACCENT = "#1d7fff"


def format_price(value: float) -> str:
    """German number format (8.075,00), like the tables in the dashboard."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _price_label(cx: float, value: str, caption: str) -> str:
    """Bold price with its caption (Low / High) under the bar. White outline copies go
    underneath so a connector line passing behind the text does not cut through it (no
    paint-order: not every renderer supports it)."""
    price = f'x="{cx:.1f}" y="{BAR_Y + 22}" text-anchor="middle" font-size="13" font-weight="700"'
    note = f'x="{cx:.1f}" y="{BAR_Y + 36}" text-anchor="middle" font-size="11"'
    halo = 'fill="#ffffff" stroke="#ffffff" stroke-width="4"'
    return (
        # white block behind both lines, so the connector does not show in the gap between them
        f'<rect x="{cx - 28:.1f}" y="{BAR_Y + 10}" width="56" height="30" fill="#ffffff"/>'
        f'<text {price} {halo}>{escape(value)}</text>'
        f'<text {note} {halo}>{escape(caption)}</text>'
        f'<text {price} fill="{TEXT}">{escape(value)}</text>'
        f'<text {note} fill="{MUTED}">{escape(caption)}</text>'
    )


def _box(cx: float, top: float, value: str, label: str, stroke: str) -> str:
    left = min(max(cx - BOX_W / 2, 2), WIDTH - BOX_W - 2)
    return (
        f'<rect x="{left:.1f}" y="{top:.1f}" width="{BOX_W}" height="{BOX_H}" rx="6" '
        f'fill="#ffffff" stroke="{stroke}" stroke-width="1.5"/>'
        f'<text x="{left + BOX_W / 2:.1f}" y="{top + 17:.1f}" text-anchor="middle" '
        f'font-size="14" font-weight="700" fill="{TEXT}">{escape(value)}</text>'
        f'<text x="{left + BOX_W / 2:.1f}" y="{top + 32:.1f}" text-anchor="middle" '
        f'font-size="11" fill="{MUTED}">{escape(label)}</text>'
    )


def price_target_svg(
    low: Optional[float],
    mean: Optional[float],
    high: Optional[float],
    current: Optional[float] = None,
    currency: str = "",
    current_source: str = "",
) -> str:
    """The SVG, or "" when there is no target to draw. current_source (Ask, Bid, Last) says
    which quote the current price is and is written next to its label."""
    if low is None or mean is None or high is None:
        return ""

    values = [low, mean, high] + ([current] if current is not None else [])
    lo, hi = min(values), max(values)
    if hi - lo <= 0:  # one analyst, or all targets equal: give the axis some width
        pad = max(abs(mean) * 0.1, 1.0)
        lo, hi = lo - pad, hi + pad
    plot_w = WIDTH - 2 * MARGIN_X

    def x(value: float) -> float:
        return MARGIN_X + (value - lo) / (hi - lo) * plot_w

    x_low, x_mean, x_high = x(low), x(mean), x(high)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" role="img" aria-label="Analyst price targets" '
        f'font-family="Arial, sans-serif">',
        # the full axis, with the analysts' range drawn thicker on top
        f'<line x1="{MARGIN_X}" y1="{BAR_Y}" x2="{WIDTH - MARGIN_X}" y2="{BAR_Y}" '
        f'stroke="{BAR}" stroke-width="6" stroke-linecap="round"/>',
        f'<line x1="{x_low:.1f}" y1="{BAR_Y}" x2="{x_high:.1f}" y2="{BAR_Y}" '
        f'stroke="#94a3b8" stroke-width="6" stroke-linecap="round"/>',
        # average: connector and box above the bar
        f'<line x1="{x_mean:.1f}" y1="{BAR_Y - 12}" x2="{x_mean:.1f}" y2="{BAR_Y}" '
        f'stroke="{ACCENT}" stroke-width="1.5"/>',
        _box(x_mean, BAR_Y - 12 - BOX_H, format_price(mean), "Average", ACCENT),
        f'<circle cx="{x_mean:.1f}" cy="{BAR_Y}" r="6" fill="{ACCENT}" stroke="#ffffff" stroke-width="2"/>',
        # low and high end points with their labels under the bar
        f'<circle cx="{x_low:.1f}" cy="{BAR_Y}" r="5" fill="{TEXT}"/>',
        f'<circle cx="{x_high:.1f}" cy="{BAR_Y}" r="5" fill="{TEXT}"/>',
        _price_label(x_low, format_price(low), "Low"),
        _price_label(x_high, format_price(high), "High"),
    ]
    if current is not None:
        x_cur = x(current)
        # inserted right after the axis so the end labels are drawn over it
        parts.insert(
            3,
            f'<line x1="{x_cur:.1f}" y1="{BAR_Y}" x2="{x_cur:.1f}" y2="{BAR_Y + 46}" '
            f'stroke="{TEXT}" stroke-width="1.5"/>',
        )
        parts += [
            f'<circle cx="{x_cur:.1f}" cy="{BAR_Y}" r="5" fill="#ffffff" stroke="{TEXT}" stroke-width="2"/>',
            _box(x_cur, BAR_Y + 46, format_price(current), f"Current ({current_source})" if current_source else "Current", TEXT),
        ]
    if currency:
        parts.append(
            f'<text x="{WIDTH - 4}" y="14" text-anchor="end" font-size="11" '
            f'fill="{MUTED}">{escape(currency)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# Yahoo's order, top of the stack first.
RECOMMENDATION_STACK = (
    ("strongBuy", "Strong Buy", "#16a34a"),
    ("buy", "Buy", "#86b36b"),
    ("hold", "Hold", "#d4a72c"),
    ("sell", "Sell", "#e07b4f"),
    ("strongSell", "Strong Sell", "#dc2626"),
)


def recommendations_svg(rows: Sequence[dict[str, Any]]) -> str:
    """Stacked bar per month (Strong Buy on top), total above each bar, legend on the right.
    "" when there is nothing to draw."""
    if not rows:
        return ""
    base_y, top_y = 128, 36  # bars grow up from base_y; the tallest one reaches top_y
    area_left, area_right = 14, WIDTH - 118
    slot = (area_right - area_left) / len(rows)
    bar_w = min(44.0, slot * 0.62)
    totals = [sum(int(row.get(key) or 0) for key, _l, _c in RECOMMENDATION_STACK) for row in rows]
    unit = (base_y - top_y) / max(max(totals), 1)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" '
        f'role="img" aria-label="Analyst recommendations" font-family="Arial, sans-serif">'
    ]
    for index, (row, total) in enumerate(zip(rows, totals)):
        cx = area_left + slot * (index + 0.5)
        left = cx - bar_w / 2
        y = base_y
        for key, _label, color in reversed(RECOMMENDATION_STACK):  # bottom segment (strongSell) first
            count = int(row.get(key) or 0)
            if not count:
                continue
            height = count * unit
            y -= height
            parts.append(f'<rect x="{left:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{height:.1f}" fill="{color}"/>')
            if height >= 13:
                parts.append(
                    f'<text x="{cx:.1f}" y="{y + height / 2 + 4:.1f}" text-anchor="middle" '
                    f'font-size="11" fill="#ffffff" font-weight="700">{count}</text>'
                )
        parts.append(
            f'<text x="{cx:.1f}" y="{y - 6:.1f}" text-anchor="middle" font-size="13" '
            f'font-weight="700" fill="{TEXT}">{total}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 18}" text-anchor="middle" font-size="12" '
            f'fill="{MUTED}">{escape(str(row.get("month") or ""))}</text>'
        )
    for index, (_key, label, color) in enumerate(RECOMMENDATION_STACK):
        ly = 48 + index * 20
        parts.append(f'<circle cx="{WIDTH - 104}" cy="{ly}" r="5" fill="{color}"/>')
        parts.append(f'<text x="{WIDTH - 94}" y="{ly + 4}" font-size="12" fill="{TEXT}">{escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _card(title: str, body: str) -> str:
    return (
        '<div class="card">'
        f'<div class="title">{escape(title)}</div>'
        f'{body or "<div class=empty>No data</div>"}'
        "</div>"
    )


def target_cards_html(
    price_svg: str, recommendations: Sequence[dict[str, Any]]
) -> str:
    """One self-contained page with the two cards stacked (price targets above recommendations).
    Meant for components.html: an iframe is not run through Streamlit's HTML sanitiser, which
    is what dropped the bare SVG when it was passed to st.html."""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><style>"
        "html,body{margin:0;padding:0;background:transparent;font-family:Arial,sans-serif;}"
        ".row{display:flex;flex-direction:column;gap:12px;}"
        f".card{{flex:1 1 0;min-width:0;background:#ffffff;border:1px solid #dbe3ec;border-radius:10px;"
        "padding:12px 14px 6px;box-sizing:border-box;}"
        f".title{{font-size:15px;font-weight:700;color:{TEXT};margin-bottom:4px;}}"
        f".empty{{font-size:13px;color:{MUTED};padding:40px 0;text-align:center;}}"
        # The cards are stacked, so each is as wide as the dialog; keep the charts at a
        # readable size instead of stretching them across it.
        "svg{display:block;max-width:480px;margin:0 auto;}"
        "</style></head><body><div class=\"row\">"
        + _card("Analyst Price Targets", price_svg)
        + _card("Analyst Recommendations", recommendations_svg(recommendations))
        + "</div></body></html>"
    )
