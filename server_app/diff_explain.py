"""Walk-through of "% Differenz zum Vortageskurs" for one table row.

The dashboard's day change is (quote / previous close - 1) * 100, where the quote is the Ask
(Bid when Ask is missing). This module turns the row's raw CSV values into the numbers and the
formula lines the click dialog shows. It does no I/O and imports nothing from Streamlit, so it
can be tested on its own. All texts are German, like the rest of the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

DAY_CHANGE_COLUMN = "% Differenz zum Vortageskurs"
# The Top list stores the basis as "Diff Basis", the watchlist CSVs as "DIFF_BASIS".
BASIS_KEYS = ("DIFF_BASIS", "Diff Basis")
# Table value is rounded to 2 decimals; anything beyond this is a stale or different input.
TOLERANCE_PCT = 0.015


def parse_number(value: Any) -> Optional[float]:
    """German ("1.234,56"), plain ("1234.56") or already numeric; None when blank/garbage."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number != number else number
    text = str(value).strip().replace("%", "").replace("EUR", "").replace("USD", "").strip()
    if not text or text == "-":
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return None if number != number else number


def fmt(number: float, decimals: int = 2, signed: bool = False) -> str:
    text = f"{abs(number):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if number < 0:
        return "−" + text
    return ("+" + text) if signed else text


@dataclass
class DiffExplanation:
    ok: bool
    title: str = "Differenz zum Vortageskurs"
    message: str = ""  # shown when ok is False
    inputs: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    steps: list[str] = field(default_factory=list)  # formula lines, in order
    result_abs: Optional[float] = None
    result_pct: Optional[float] = None
    currency: str = ""
    notes: list[str] = field(default_factory=list)


def _text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value).strip()


def _missing(message: str, *notes: str) -> DiffExplanation:
    return DiffExplanation(ok=False, message=message, notes=list(notes))


def _date_de(iso: str) -> str:
    parts = str(iso or "").split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 and all(parts) else str(iso or "")


def explain_day_change(raw: Mapping[str, Any], stored: Optional[Mapping[str, Any]] = None) -> DiffExplanation:
    """Explain the day change of one row from its raw CSV values (Ask, Bid, DEV, REF_PREV_CLOSE,
    REF_CURRENCY, DIFF_BASIS, "% Differenz zum Vortageskurs"). `stored` is the API's /prev-close
    answer ({"found", "ref_eur", "close": {value, currency, date, source}}), used for rows whose
    CSV has no REF_PREV_CLOSE (the Top list)."""
    table_pct = parse_number(raw.get(DAY_CHANGE_COLUMN))
    basis = next((_text(raw, key) for key in BASIS_KEYS if _text(raw, key)), "")
    ask, bid = parse_number(raw.get("Ask")), parse_number(raw.get("Bid"))
    ask = ask if ask and ask > 0 else None
    bid = bid if bid and bid > 0 else None
    dev = _text(raw, "DEV").upper()

    if table_pct is None:
        return _missing(
            "Keine Berechnung möglich: Markt geschlossen oder Daten fehlen.",
            "Für diese Zeile liegt kein Wert für die Differenz zum Vortageskurs vor.",
        )

    # The price the stored percentage was based on. A row without any basis is an old one: Ask.
    if basis in ("", "Ask"):
        label, quote = "Ask", ask
    elif basis == "Bid":
        label, quote = "Bid (Ask fehlte)", bid
    else:
        # "Close" / "Page" / "Last": the value did not come from a live quote of this row.
        source = {
            "Close": "dem letzten Schlusskurs (kein Live-Kurs verfügbar)",
            "Page": "dem Wert der Quellseite (keine Kurshistorie verfügbar)",
            "Last": "dem letzten gehandelten Kurs (kein Ask/Bid verfügbar)",
        }.get(basis, f"„{basis}“")
        return DiffExplanation(
            ok=False,
            message=f"Der Wert {fmt(table_pct, signed=True)} % wurde nicht aus dem Ask berechnet, sondern aus {source}.",
            notes=["Eine Rechnung mit Vortagesschlusskurs und Ask ist für diese Zeile daher nicht möglich."],
        )
    if quote is None:
        return _missing(
            "Keine Berechnung möglich: Markt geschlossen oder Daten fehlen.",
            f"Es ist aktuell kein {label.split(' ')[0]}-Kurs vorhanden.",
        )

    ref_currency = _text(raw, "REF_CURRENCY").upper() or dev or "EUR"
    reference = parse_number(raw.get("REF_PREV_CLOSE"))
    reference_derived = False
    close_row: Optional[tuple[str, str]] = None  # (label, value) of the raw close, when stored
    close_in_eur: Optional[str] = None
    if (reference is None or reference <= 0) and stored and stored.get("found"):
        ref_eur = parse_number(stored.get("ref_eur"))
        if ref_eur and ref_eur > 0:
            reference, ref_currency = ref_eur, "EUR"
            raw_close = stored.get("close") if isinstance(stored.get("close"), Mapping) else None
            value = parse_number(raw_close.get("value")) if raw_close else None
            if raw_close and value:
                source = f", {raw_close['source']}" if raw_close.get("source") else ""
                when = f" {_date_de(raw_close.get('date'))}" if raw_close.get("date") else ""
                close_row = (f"Close{when}{source}", f"{fmt(value)} {raw_close.get('currency') or ''}".strip())
                if (raw_close.get("currency") or "EUR") != "EUR":
                    close_in_eur = f"{fmt(ref_eur)} EUR"
    if reference is None or reference <= 0:
        # The Top list keeps no reference column. Undo the percentage so the formula can still be
        # shown; only possible when the quote is in the reference currency (equities in EUR).
        if dev and dev != ref_currency:
            return _missing(
                "Keine Berechnung möglich: Vortagesschlusskurs nicht gespeichert.",
                f"Der Kurs ist in {dev}, der Vortagesschlusskurs ließe sich ohne Wechselkurs nicht zurückrechnen.",
            )
        if table_pct <= -100:
            return _missing("Keine Berechnung möglich: Vortagesschlusskurs nicht gespeichert.")
        reference, reference_derived = quote / (1 + table_pct / 100), True

    # Quote in another currency than the reference: the pipeline converts at the live rate. The
    # rate itself is not stored, so show the one the stored percentage implies.
    converted = quote
    steps_currency: list[str] = []
    if dev and dev != ref_currency:
        converted = reference * (1 + table_pct / 100)
        rate = converted / quote
        steps_currency = [
            f"{label} umgerechnet = {fmt(quote)} {dev} × {fmt(rate, 4)} = {fmt(converted)} {ref_currency}",
        ]

    absolute = converted - reference
    pct = (converted / reference - 1) * 100
    explanation = DiffExplanation(ok=True, result_abs=absolute, result_pct=pct, currency=ref_currency)
    if close_row:
        explanation.inputs = [("Vortagesschlusskurs (" + close_row[0] + ")", close_row[1])]
        if close_in_eur:
            explanation.inputs.append(("Vortagesschlusskurs in EUR (umgerechnet)", close_in_eur))
    else:
        explanation.inputs = [
            ("Vortagesschlusskurs (Close)", f"{fmt(reference)} {ref_currency}" + (" *" if reference_derived else "")),
        ]
    explanation.inputs.append((label, f"{fmt(quote)} {dev or ref_currency}"))
    if raw.get("Ask Time") and label == "Ask":
        explanation.inputs.append(("Stand Ask", _text(raw, "Ask Time")))
    explanation.steps = [
        *steps_currency,
        f"Differenz = {fmt(converted)} − {fmt(reference)} = {fmt(absolute, signed=True)} {ref_currency}",
        f"Prozent = ({fmt(converted)} − {fmt(reference)}) ÷ {fmt(reference)} × 100 = {fmt(pct, signed=True)} %",
    ]
    if reference_derived:
        explanation.notes.append(
            "* Für diese Liste ist der Vortagesschlusskurs nicht separat gespeichert; "
            "er wurde aus Ask und dem angezeigten Prozentwert zurückgerechnet."
        )
    if abs(pct - table_pct) > TOLERANCE_PCT:
        explanation.notes.append(
            f"Die Tabelle zeigt {fmt(table_pct, signed=True)} %. Der Ask hat sich seit der letzten "
            "Berechnung geändert; die Tabelle wird beim nächsten Update aktualisiert."
        )
    return explanation
