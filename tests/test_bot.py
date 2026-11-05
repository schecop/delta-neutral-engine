"""The engine bot on fake venues and a fake messenger: what the operator sees and can do."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delta_neutral.bot import EngineBot
from delta_neutral.executor import CycleState
from delta_neutral.telegram import Buttons
from tests.test_executor import Clock, _setup

CHAT = 4242
THURSDAY = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
FRIDAY_IN_WINDOW = datetime(2026, 11, 6, 20, 45, tzinfo=UTC)  # example config: from 20:30 UTC


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Buttons | None]] = []
        self.answered: list[str] = []

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        self.sent.append((chat_id, text, buttons))

    async def answer(self, callback_id: str, text: str = "") -> None:
        self.answered.append(callback_id)

    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


def _bot(tmp_path: Path) -> tuple[EngineBot, FakeMessenger, Clock]:
    clock = Clock()
    clock.now = THURSDAY
    config, venues, db = _setup(tmp_path, clock)
    messenger = FakeMessenger()
    return EngineBot(config, venues, db, messenger, CHAT, clock=clock), messenger, clock


def _message(text: str, chat: int = CHAT) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def _press(data: str, chat: int = CHAT) -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"chat": {"id": chat}},
        },
    }


def _state(bot: EngineBot, cycle_id: int = 1) -> str:
    row = bot.executor._db.execute("SELECT state FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    return str(row[0])


def test_other_chats_are_ignored(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD", chat=666)))
    asyncio.run(bot.handle(_press("abort:1", chat=666)))
    assert messenger.sent == [] and messenger.answered == []
    assert bot.executor._db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 0


def test_plan_then_the_filled_button_opens_the_cycle(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    chat, text, buttons = messenger.sent[0]
    assert chat == CHAT and text.startswith("ACTION cycle 1 open:") and "XAU-PERP" in text
    assert buttons is not None
    (filled, data), _abort = buttons[0]
    assert filled.startswith("Filled at ~") and data.startswith("fill:1:")
    assert len(data.encode()) <= 64  # Telegram's callback_data limit

    asyncio.run(bot.handle(_press(data)))
    assert messenger.answered == ["cb1"]
    assert _state(bot) == CycleState.OPEN


def test_fill_command_with_another_price(tmp_path: Path) -> None:
    bot, _, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 1997.5")))
    assert _state(bot) == CycleState.OPEN
    price = bot.executor._db.execute(
        "SELECT price FROM legs WHERE venue = 'variational' AND phase = 'open'"
    ).fetchone()[0]
    assert price == "1997.5"


def test_errors_are_answered_not_raised(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/close 99")))
    asyncio.run(bot.handle(_message("/fill 1 not-a-price")))
    asyncio.run(bot.handle(_message("/what")))
    texts = messenger.texts()
    assert texts[0] == "error: no cycle 99"
    assert texts[1].startswith("error:")
    assert texts[2].startswith("/plan")  # unknown command: the help


def test_the_engine_pass_alerts_and_sends_the_close_instruction(tmp_path: Path) -> None:
    bot, messenger, clock = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 1998")))
    messenger.sent.clear()

    clock.now = FRIDAY_IN_WINDOW  # past the hold and inside the weekend window
    asyncio.run(bot.engine_pass())
    texts = messenger.texts()
    assert any(t.startswith("ACTION cycle 1 close:") for t in texts)
    assert _state(bot) == CycleState.CLOSING
    # the close instruction is sent once, not again on the next pass
    messenger.sent.clear()
    asyncio.run(bot.engine_pass())
    assert not any(t.startswith("ACTION") for t in messenger.texts())


def test_risk_events_are_forwarded_once(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 1998")))
    bot.monitor._positions = _empty_positions  # qfex lost its leg
    messenger.sent.clear()
    asyncio.run(bot.engine_pass())
    alerts = [t for t in messenger.texts() if t.startswith("RISK")]
    assert len(alerts) == 1 and "leg_missing -> close_cycle" in alerts[0]
    asyncio.run(bot.engine_pass())
    assert len([t for t in messenger.texts() if t.startswith("RISK")]) == 1


async def _empty_positions() -> dict[str, dict[str, Any]]:
    return {"qfex": {}}
