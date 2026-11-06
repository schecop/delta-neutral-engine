"""What the operator reads on Telegram: short cards in Telegram's HTML subset.

The bot decides when to send; this module decides what the message looks like. Rules:
- the thing to do comes first and in bold: phase, venue, BUY/SELL, size, symbol;
- direction has a colour (🟢 BUY, 🔴 SELL), the most costly thing to get wrong by hand;
- sizes are `<code>`, which Telegram copies on tap, so the size is pasted, not retyped;
- prices and sizes are in the venue's own units and symbols, as its app shows them;
- times are the operator's local time, the clock their venue apps show;
- anything from outside (venue names, error text) is escaped.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from delta_neutral.executor import ManualInstruction
from delta_neutral.instruments import InstrumentRegistry
from delta_neutral.scorer import Candidate
from delta_neutral.telegram import Buttons

TOP = 5


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def num(d: Decimal) -> str:
    """A Decimal without trailing zeros or exponent: 0.0736, 4069.54, 100."""
    return format(d.normalize(), "f")


def usd(d: Decimal, places: int = 3) -> str:
    """$0.430, -$0.002 (a sign before the $, not after it)."""
    return f"{'-' if d < 0 else ''}${abs(d):.{places}f}"


def local_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")


def pre(text: str) -> str:
    """Fixed-width text (tables), escaped."""
    return f"<pre>{esc(text)}</pre>"


def error(text: object) -> str:
    return f"⛔ error: {esc(text)}"


def _side(buys: bool) -> str:
    return "🟢 BUY" if buys else "🔴 SELL"


def instruction(i: ManualInstruction) -> str:
    buys = i.action == "BUY"
    head = "▶️ OPEN" if i.phase == "open" else "⏹ CLOSE"
    closes = "" if i.phase == "open" else f" (closes your {'short' if buys else 'long'})"
    return "\n".join(
        [
            f"<b>{head} · cycle {i.cycle_id}</b>",
            f"<b>{esc(i.venue.upper())}</b>",
            f"<b>{_side(buys)}</b> <code>{num(i.venue_size)}</code> <b>{esc(i.symbol)}</b>"
            + closes,
            f"price ~{num(i.venue_price)} · ≈ ${i.notional_usd:,.0f}",
            "",
            "Tap ✅ the moment it fills: unhedged time counts from the fill.",
            f"<i>Exact price or time later: /amend {i.cycle_id} {esc(i.venue)} "
            "price=P at=HH:MM:SS</i>",
        ]
    )


def buttons(i: ManualInstruction) -> Buttons:
    price = format(i.venue_price, "f")
    row = [(f"✅ {i.venue} filled ~{num(i.venue_price)}", f"fill:{i.cycle_id}:{i.venue}:{price}")]
    if i.phase == "open":
        row.append(("✖ Abort", f"abort:{i.cycle_id}"))
    return [row]


def cycle(db: sqlite3.Connection, registry: InstrumentRegistry, cycle_id: int) -> str:
    """The cycle as it stands: which legs filled (and when), what is still waiting, the result."""
    c = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if c is None:
        return f"cycle {cycle_id}: not found"
    legs = db.execute(
        "SELECT * FROM legs WHERE cycle_id = ? ORDER BY phase = 'close', side", (cycle_id,)
    ).fetchall()
    instrument, state = c["instrument"], c["state"]

    def leg_line(leg: sqlite3.Row) -> str:
        scale = registry.contract_size(instrument, leg["venue"])
        buys = (leg["side"] == "long") == (leg["phase"] == "open")
        what = f"{esc(leg['venue'])} {_side(buys)} {num(Decimal(leg['size']) / scale)}"
        if leg["price"] is None:
            return f"⏳ {what} · waiting"
        price = num(Decimal(leg["price"]) * scale)
        return f"✅ {what} @ {price} · {local_time(leg['filled_at'])}"

    def apart(phase: str) -> str:
        times = [datetime.fromisoformat(r["filled_at"]) for r in legs if r["phase"] == phase]
        return f"{(max(times) - min(times)).total_seconds():.0f}s"

    title = f"cycle {cycle_id} · {esc(instrument)}"
    if state == "aborted":
        return f"✖ <b>{title} · ABORTED</b>"
    if state in ("planned", "opening", "closing"):
        phase = "close" if state == "closing" else "open"
        lines = [f"⏳ <b>{title} · {state.upper()}</b>"]
        lines += [leg_line(r) for r in legs if r["phase"] == phase]
        return "\n".join(lines)
    if state == "open":
        lines = [f"✅ <b>{title} · OPEN</b>"]
        lines += [leg_line(r) for r in legs if r["phase"] == "open"]
        lines.append(f"legs {apart('open')} apart · close at {local_time(c['close_due_at'])}")
        return "\n".join(lines)
    s = json.loads(c["summary"])
    realised, predicted = Decimal(s["realised_cost_usd"]), Decimal(s["predicted_cost_usd"])
    error_usd = realised - predicted
    pct = f", {error_usd / predicted:+.1%}" if predicted else ""
    return "\n".join(
        [
            f"🏁 <b>{title} · CLOSED</b> · held {s['held_minutes']:.0f} min",
            f"<b>cost {usd(realised)}</b> vs predicted {usd(predicted)} "
            f"({'+' if error_usd >= 0 else ''}{usd(error_usd)}{pct})",
            f"price {usd(-Decimal(s['price_pnl_usd']))} · fees {usd(Decimal(s['fees_usd']))} · "
            f"funding {usd(Decimal(s['funding_usd_estimated']))}",
            f"legs apart: open {apart('open')} · close {apart('close')}",
        ]
    )


def ranking(candidates: list[Candidate], notional: Decimal, hold_minutes: int) -> str:
    """The top candidates, three short lines each: narrow enough for a phone."""
    if not candidates:
        return "no candidate: no instrument has quotes on two venues right now"
    shown = min(TOP, len(candidates))
    lines = [f"📊 <b>Top {shown} of {len(candidates)}</b> · ${notional}/leg · {hold_minutes} min"]
    for n, c in enumerate(candidates[:TOP], 1):
        per_usd = "" if c.score is None else f" · reward/$ {c.score:.1f}"
        lines += [
            "",
            f"<b>{n}. {esc(c.instrument)} · {usd(c.cost_usd)}</b>{per_usd}",
            f"long {esc(c.long_venue)} · short {esc(c.short_venue)}",
            f"spread {usd(c.spread_usd)} · fees {usd(c.fees_usd)} · funding {usd(c.funding_usd)}",
        ]
        if c.flags:
            lines.append(f"<i>{esc(', '.join(c.flags))}</i>")
    return "\n".join(lines)


_RISK_ICON = {"confirm_fill": "⏳", "exceeded": "⚠️"}
_RISK_HINT = {
    "exceeded": "Tap ✅ the moment a leg fills; fix price or time later with /amend.",
}


def risk(rule: str, action: str, message: str) -> str:
    lines = [
        f"{_RISK_ICON.get(action, '🚨')} <b>RISK · {esc(rule)} → {esc(action)}</b>",
        esc(message),
    ]
    if action in _RISK_HINT:
        lines.append(f"<i>{_RISK_HINT[action]}</i>")
    return "\n".join(lines)
