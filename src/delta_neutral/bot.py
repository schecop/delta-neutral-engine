"""Engine bot: the operator's Telegram console and the engine loop, in one process.

    uv run python -m delta_neutral.bot run       # needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    uv run python -m delta_neutral.bot chat-id   # setup: print the chat ids that wrote to the bot

DRY-RUN: drives the executor, which places no orders.

Two loops share one event loop and one set of venue connections (so the API leg fills in
milliseconds, not after a fresh WebSocket handshake):
- polling: commands and button presses from the operator's chat, and only that chat;
- engine: every few seconds, close cycles past their hold, run the risk monitor, and forward
  each new risk event as an alert.

Commands: /plan [INSTRUMENT], /fill CYCLE PRICE [SIZE], /close CYCLE, /abort CYCLE, /status,
/events. A manual instruction comes with buttons: "Filled at ~P" confirms at the quoted price;
another price goes through /fill.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from delta_neutral.config import AppConfig, load_config
from delta_neutral.env import load_env, require
from delta_neutral.executor import (
    CycleError,
    Executor,
    ManualInstruction,
    open_engine_db,
    plan_best,
    status_text,
)
from delta_neutral.risk import SCHEMA as RISK_SCHEMA
from delta_neutral.risk import RiskMonitor, paper_positions
from delta_neutral.telegram import Buttons, TelegramClient
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.factory import build_venues

log = logging.getLogger(__name__)

HELP = (
    "/plan [INSTRUMENT] - score now and open the best cycle\n"
    "/fill CYCLE PRICE [SIZE] - confirm a manual leg at the venue's fill price\n"
    "/close CYCLE - close before the hold ends\n"
    "/abort CYCLE - cancel a cycle before any fill\n"
    "/status - recent cycles\n"
    "/events - recent risk events"
)


class Messenger(Protocol):
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None: ...

    async def answer(self, callback_id: str, text: str = "") -> None: ...


def _buttons(i: ManualInstruction) -> Buttons:
    price = format(i.venue_price, "f")
    row = [(f"Filled at ~{price}", f"fill:{i.cycle_id}:{price}")]
    if i.phase == "open":
        row.append(("Abort", f"abort:{i.cycle_id}"))
    return [row]


class EngineBot:
    def __init__(
        self,
        config: AppConfig,
        venues: Mapping[str, VenueAdapter],
        db: sqlite3.Connection,
        messenger: Messenger,
        chat_id: int,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._venues = venues
        self._db = db
        self._tg = messenger
        self._chat_id = chat_id
        self.executor = Executor(config, venues, db, clock=clock)
        self.monitor = RiskMonitor(
            config, self.executor, db, positions=paper_positions(db), clock=clock
        )
        db.executescript(RISK_SCHEMA)
        # alerts start from now: events recorded before this process are not replayed
        (last,) = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM risk_events").fetchone()
        self._last_event: int = last
        self._announced_closes: set[int] = set()  # closing cycles whose instruction was sent

    # -- operator input -------------------------------------------------------------------------

    async def handle(self, update: Mapping[str, Any]) -> None:
        """One Telegram update. Anything not from the operator's chat is dropped unread."""
        callback = update.get("callback_query")
        message = callback["message"] if callback else update.get("message")
        chat = (message or {}).get("chat", {}).get("id")
        if chat != self._chat_id:
            log.warning("ignored an update from chat %s", chat)
            return
        try:
            if callback:
                await self._tg.answer(callback["id"])
                await self._on_button(str(callback.get("data", "")))
            else:
                await self._on_command(str(message.get("text", "")) if message else "")
        except (CycleError, InvalidOperation, ValueError, KeyError) as exc:
            await self._say(f"error: {exc}")
        except Exception as exc:  # a venue or network failure: tell the operator, keep running
            log.exception("command failed")
            await self._say(f"failed: {type(exc).__name__}: {exc}")

    async def _on_command(self, text: str) -> None:
        command, *args = text.split() or [""]
        match command.split("@")[0]:
            case "/plan":
                await self._instruct(
                    await plan_best(self.executor, self._config, self._venues, *args[:1])
                )
                await self._say(status_text(self._db).splitlines()[0])
            case "/fill":
                cycle, price, *size = args
                await self._instruct(
                    await self.executor.confirm_manual(
                        int(cycle), Decimal(price), Decimal(size[0]) if size else None
                    )
                )
                await self._say(self._cycle_line(int(cycle)))
            case "/close":
                await self._instruct(await self.executor.request_close(int(args[0])))
            case "/abort":
                self.executor.abort(int(args[0]), "operator")
                await self._say(self._cycle_line(int(args[0])))
            case "/status":
                await self._say(status_text(self._db))
            case "/events":
                await self._say(self._events_text())
            case _:
                await self._say(HELP)

    async def _on_button(self, data: str) -> None:
        kind, cycle, *rest = data.split(":")
        if kind == "fill":
            await self._instruct(await self.executor.confirm_manual(int(cycle), Decimal(rest[0])))
            await self._say(self._cycle_line(int(cycle)))
        elif kind == "abort":
            self.executor.abort(int(cycle), "operator")
            await self._say(self._cycle_line(int(cycle)))

    # -- the engine loop ------------------------------------------------------------------------

    async def engine_pass(self) -> None:
        """Close due cycles, run the risk rules, and alert on every new risk event."""
        await self._instruct(await self.executor.close_due())
        await self.monitor.check()
        rows = self._db.execute(
            "SELECT rowid, rule, action, message FROM risk_events WHERE rowid > ? ORDER BY rowid",
            (self._last_event,),
        ).fetchall()
        for rowid, rule, action, message in rows:
            await self._say(f"RISK {rule} -> {action}: {message}")
            self._last_event = rowid
        # actions the monitor took (a close) may need the operator's manual leg
        for cycle_id in {
            r[0] for r in self._db.execute("SELECT id FROM cycles WHERE state = 'closing'")
        }:
            if cycle_id not in self._announced_closes:
                self._announced_closes.add(cycle_id)
                await self._instruct(await self.executor.resume(cycle_id))

    # -- helpers --------------------------------------------------------------------------------

    async def _instruct(self, instructions: list[ManualInstruction]) -> None:
        for i in instructions:
            if i.phase == "close":
                self._announced_closes.add(i.cycle_id)
            await self._tg.send(self._chat_id, "ACTION " + i.message(), _buttons(i))

    async def _say(self, text: str) -> None:
        await self._tg.send(self._chat_id, text)

    def _cycle_line(self, cycle_id: int) -> str:
        line = next(
            (ln for ln in status_text(self._db).splitlines() if ln.startswith(f"#{cycle_id} ")),
            None,
        )
        return line or f"cycle {cycle_id}"

    def _events_text(self) -> str:
        rows = self._db.execute(
            "SELECT at, rule, action, message FROM risk_events ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        return "\n".join(f"{r[0][:19]} {r[1]} {r[2]}: {r[3]}" for r in rows) or "no risk events"


async def _poll(bot: EngineBot, tg: TelegramClient) -> None:
    offset: int | None = None
    while True:
        try:
            updates = await tg.get_updates(offset)
        except Exception as exc:  # network blips: wait and poll again
            log.warning("telegram poll failed: %s", exc)
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1  # confirms it: Telegram will not resend it
            await bot.handle(update)


async def _engine(bot: EngineBot, every: float) -> None:
    while True:
        try:
            await bot.engine_pass()
        except Exception:
            log.exception("engine pass failed")
        await asyncio.sleep(every)


async def _run(config: AppConfig, every: float) -> None:
    tg = TelegramClient(require("TELEGRAM_BOT_TOKEN"))
    chat_id = int(require("TELEGRAM_CHAT_ID"))
    venues = build_venues(config)
    db = open_engine_db(config.executor.db_path)
    try:
        bot = EngineBot(config, venues, db, tg, chat_id)
        await tg.send(chat_id, f"engine up ({config.mode}). /help for commands")
        await asyncio.gather(_poll(bot, tg), _engine(bot, every))
    finally:
        for venue in venues.values():
            await venue.aclose()
        await tg.aclose()
        db.close()


async def _chat_ids() -> None:
    tg = TelegramClient(require("TELEGRAM_BOT_TOKEN"))
    try:
        updates = await tg.get_updates(None, timeout=0)
        seen = {(u.get("message") or {}).get("chat", {}).get("id") for u in updates} - {None}
        print("\n".join(map(str, seen)) or "no messages yet: write anything to your bot first")
    finally:
        await tg.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=["run", "chat-id"])
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--every", type=float, default=10.0, help="seconds between engine passes")
    args = parser.parse_args()
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log would print the token
    with contextlib.suppress(KeyboardInterrupt):
        if args.command == "chat-id":
            asyncio.run(_chat_ids())
        else:
            asyncio.run(_run(load_config(args.config), args.every))


if __name__ == "__main__":
    main()
