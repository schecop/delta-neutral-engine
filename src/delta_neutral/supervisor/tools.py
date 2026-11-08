"""The supervisor's tools: read-only queries over engine.db, plus one that only *proposes*.

Read-only is enforced, not promised: the database is opened with SQLite's `mode=ro`, so a
write from here fails in SQLite itself. Every figure the model may quote is computed here, in
Decimal, and returned as text with its unit in the key name (`cost_usd`), so the model repeats
numbers instead of doing arithmetic (rule: numbers come from tools, never from the model).

`propose_action` writes nothing either: it returns a proposal the bot shows with Approve and
Reject buttons. Only the operator's tap runs the action, through the executor.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from delta_neutral.llm.base import ToolSpec

ACTIONS = ("close_cycle", "abort_cycle")


@dataclass(frozen=True)
class Proposal:
    action: str  # one of ACTIONS
    cycle_id: int
    reason: str


def open_readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _since(hours: float, now: datetime) -> str:
    return (now - timedelta(hours=hours)).isoformat()


def _usd(value: Decimal) -> str:
    return f"{value:.4f}"


def _days(hours: float) -> str:
    days = (Decimal(str(hours)) / 24).quantize(Decimal("0.1")).normalize()
    return f"{days:f}"


class SupervisorTools:
    def __init__(
        self, db: sqlite3.Connection, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._db = db
        self._clock = clock
        self.proposals: list[Proposal] = []  # collected during one run, shown by the bot

    def now(self) -> str:
        """The time the model is told it is: the same clock the tools' windows use."""
        return self._clock().isoformat(timespec="seconds")

    # -- the tools ------------------------------------------------------------------------------

    def list_cycles(self, limit: int = 10, state: str | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM cycles"
        params: list[Any] = []
        if state:
            sql += " WHERE state = ?"
            params.append(state)
        rows = self._db.execute(f"{sql} ORDER BY id DESC LIMIT ?", (*params, min(limit, 50)))
        return {"cycles": [self._cycle_row(r) for r in rows]}

    def get_cycle(self, cycle_id: int) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if row is None:
            return {"error": f"no cycle {cycle_id}"}
        legs = [
            {
                "phase": r["phase"],
                "venue": r["venue"],
                "side": r["side"],
                "execution": r["execution"],
                "size": r["size"],
                "price": r["price"],
                "fee_usd": r["fee_usd"] and _usd(Decimal(r["fee_usd"])),
                "requested_at": r["requested_at"],
                "filled_at": r["filled_at"],
            }
            for r in self._db.execute(
                "SELECT * FROM legs WHERE cycle_id = ? ORDER BY requested_at", (cycle_id,)
            )
        ]
        events = [
            {"at": r["at"], "kind": r["kind"]}
            for r in self._db.execute(
                "SELECT at, kind FROM cycle_events WHERE cycle_id = ? ORDER BY rowid", (cycle_id,)
            )
        ]
        return {**self._cycle_row(row), "legs": legs, "events": events}

    def risk_events(self, hours: float = 24, limit: int = 20) -> dict[str, Any]:
        rows = self._db.execute(
            "SELECT * FROM risk_events WHERE at >= ? ORDER BY rowid DESC LIMIT ?",
            (_since(hours, self._clock()), min(limit, 50)),
        ).fetchall()
        return {
            "window_hours": hours,
            "count": len(rows),
            "events": [
                {
                    "at": r["at"],
                    "rule": r["rule"],
                    "action": r["action"],
                    "cycle_id": r["cycle_id"],
                    "venue": r["venue"],
                    "message": r["message"],
                }
                for r in rows
            ],
        }

    def cost_summary(self, hours: float = 24) -> dict[str, Any]:
        """Totals over the closed cycles that ended in the window, computed here in Decimal."""
        rows = self._db.execute(
            "SELECT id, notional_usd, summary FROM cycles "
            "WHERE state = 'closed' AND summary IS NOT NULL AND closed_at >= ? ORDER BY id",
            (_since(hours, self._clock()),),
        ).fetchall()
        zero = Decimal(0)
        predicted = realised = fees = pnl = funding = volume = zero
        worst: tuple[Decimal, int] | None = None
        for r in rows:
            s = json.loads(r["summary"])
            predicted += Decimal(s["predicted_cost_usd"])
            realised += Decimal(s["realised_cost_usd"])
            fees += Decimal(s["fees_usd"])
            pnl += Decimal(s["price_pnl_usd"])
            funding += Decimal(s["funding_usd_estimated"])
            volume += 4 * Decimal(r["notional_usd"])  # two legs, opened and closed
            error = abs(Decimal(s["error_usd"]))
            if worst is None or error > worst[0]:
                worst = (error, r["id"])
        n = len(rows)
        return {
            "window_hours": hours,
            "window_days": _days(hours),  # so "30 days" is a tool figure, not the model's maths
            "closed_cycles": n,
            "cycle_ids": [r["id"] for r in rows],
            "predicted_cost_usd": _usd(predicted),
            "realised_cost_usd": _usd(realised),
            "error_usd": _usd(realised - predicted),
            "fees_usd": _usd(fees),
            "price_pnl_usd": _usd(pnl),
            "funding_usd_estimated": _usd(funding),
            "volume_usd": _usd(volume),
            "realised_cost_per_cycle_usd": _usd(realised / n) if n else None,
            "realised_cost_bps_of_volume": f"{realised / volume * 10_000:.2f}" if volume else None,
            "largest_abs_error": (
                {"cycle_id": worst[1], "abs_error_usd": _usd(worst[0])} if worst else None
            ),
        }

    def propose_action(self, action: str, cycle_id: int, reason: str) -> dict[str, Any]:
        if action not in ACTIONS:
            return {"error": f"unknown action {action!r}; allowed: {list(ACTIONS)}"}
        row = self._db.execute("SELECT state FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if row is None:
            return {"error": f"no cycle {cycle_id}"}
        self.proposals.append(Proposal(action, cycle_id, reason))
        return {
            "status": "proposed, NOT executed",
            "next": "the operator approves or rejects it on Telegram; nothing happens until then",
            "cycle_state": row["state"],
        }

    # -- plumbing -------------------------------------------------------------------------------

    def _cycle_row(self, r: sqlite3.Row) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": r["id"],
            "instrument": r["instrument"],
            "long_venue": r["long_venue"],
            "short_venue": r["short_venue"],
            "notional_per_leg_usd": r["notional_usd"],
            "hold_minutes": r["hold_minutes"],
            "state": r["state"],
            "created_at": r["created_at"],
            "opened_at": r["opened_at"],
            "close_due_at": r["close_due_at"],
            "closed_at": r["closed_at"],
            "predicted_cost_usd": _usd(Decimal(r["predicted_cost_usd"])),
        }
        if r["summary"]:
            s = json.loads(r["summary"])
            out["result"] = {
                k: (_usd(Decimal(v)) if k.endswith("_usd") else v) for k, v in s.items()
            }
        return out

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, name, None) if name in TOOL_NAMES else None
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            result: dict[str, Any] = handler(**arguments)
        except TypeError as exc:  # wrong or missing arguments from the model
            return {"error": f"bad arguments for {name}: {exc}"}
        return result


SPECS = [
    ToolSpec(
        "list_cycles",
        "Recent hedged cycles, newest first: instrument, venues, size, state, timestamps, the "
        "predicted cost and, for closed cycles, the realised result (cost, fees, price P&L, "
        "funding, error vs prediction, hold, leg skew).",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "state": {
                    "type": "string",
                    "enum": ["planned", "opening", "open", "closing", "closed", "aborted"],
                },
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "get_cycle",
        "One cycle in full: its result plus every leg (venue, side, size, price, fee, fill "
        "time) and its event log.",
        {
            "type": "object",
            "properties": {"cycle_id": {"type": "integer"}},
            "required": ["cycle_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "risk_events",
        "Risk events in the last N hours (unhedged time, a missing leg, margin, weekend rule).",
        {
            "type": "object",
            "properties": {
                "hours": {"type": "number", "exclusiveMinimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "cost_summary",
        "Totals over cycles closed in the last N hours: count, predicted and realised cost, "
        "fees, price P&L, estimated funding, volume, cost per cycle and in bps of volume, and "
        "the cycle with the largest prediction error. Use it for any total or average.",
        {
            "type": "object",
            "properties": {"hours": {"type": "number", "exclusiveMinimum": 0}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "propose_action",
        "Propose closing or aborting a cycle. It does NOT execute anything: the operator "
        "approves or rejects the proposal on Telegram. The only way to suggest an action.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "cycle_id": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["action", "cycle_id", "reason"],
            "additionalProperties": False,
        },
    ),
]
TOOL_NAMES = {s.name for s in SPECS}
