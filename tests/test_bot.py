"""The engine bot on fake venues and a fake messenger: what the operator sees and can do."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from delta_neutral.bot import EngineBot, parse_fill_time
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
    assert chat == CHAT and text.startswith("<b>▶️ OPEN · cycle 1</b>\n<b>VARIATIONAL</b>")
    assert "<b>🔴 SELL</b> <code>0.5</code> <b>XAU-PERP</b>" in text  # size is tap-to-copy
    assert buttons is not None
    (filled, data), _abort = buttons[0]
    assert filled.startswith("✅ variational filled ~") and data.startswith("fill:1:variational:")
    assert len(data.encode()) <= 64  # Telegram's callback_data limit

    asyncio.run(bot.handle(_press(data)))
    assert messenger.answered == ["cb1"]
    assert _state(bot) == CycleState.OPEN
    card = messenger.texts()[-1]
    assert card.startswith("✅ <b>cycle 1 · XAU-USD · OPEN</b>") and "close at" in card


def test_score_lists_the_best_candidates_and_opens_nothing(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/score")))
    (text,) = messenger.texts()
    assert text.startswith("📊 <b>Top ") and "<b>1. XAU-USD · $" in text and "fees $" in text
    assert bot.executor._db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 0


def test_fill_command_with_another_price(tmp_path: Path) -> None:
    bot, _, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 1997.5")))
    assert _state(bot) == CycleState.OPEN
    price = bot.executor._db.execute(
        "SELECT price FROM legs WHERE venue = 'variational' AND phase = 'open'"
    ).fetchone()[0]
    assert price == "1997.5"


def test_fill_with_venue_and_local_time(tmp_path: Path) -> None:
    bot, _, clock = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    planned = clock.now
    clock.advance(90)
    local = (planned + timedelta(seconds=30)).astimezone()  # what the venue's history shows
    asyncio.run(bot.handle(_message(f"/fill 1 variational 1997.5 {local:%H:%M:%S}")))
    row = bot.executor._db.execute(
        "SELECT price, filled_at FROM legs WHERE venue = 'variational' AND phase = 'open'"
    ).fetchone()
    assert row[0] == "1997.5"
    assert datetime.fromisoformat(row[1]) == planned + timedelta(seconds=30)


def test_amend_corrects_a_fill_from_the_chat(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 variational 1998")))
    asyncio.run(bot.handle(_message("/amend 1 variational size=0.4 price=1997")))
    assert messenger.texts()[-1].startswith("✏️ amended variational\n")
    row = bot.executor._db.execute(
        "SELECT size, price FROM legs WHERE venue = 'variational' AND phase = 'open'"
    ).fetchone()
    assert (Decimal(row[0]), Decimal(row[1])) == (Decimal("0.4"), Decimal(1997))
    asyncio.run(bot.handle(_message("/amend 1 variational colour=red")))
    assert messenger.texts()[-1].startswith("⛔ error: usage: /amend")


def test_a_fill_time_past_now_belongs_to_yesterday() -> None:
    now = datetime(2026, 11, 5, 23, 0, 30, tzinfo=UTC)
    later_today = (now + timedelta(hours=1)).astimezone()
    assert parse_fill_time(f"{later_today:%H:%M:%S}", now) == now + timedelta(hours=1, days=-1)


def test_errors_are_answered_not_raised(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/close 99")))
    asyncio.run(bot.handle(_message("/fill 1 not-a-price")))
    asyncio.run(bot.handle(_message("/what")))
    texts = messenger.texts()
    assert texts[0] == "⛔ error: no cycle 99"
    assert texts[1].startswith("⛔ error:")
    assert texts[2].startswith("/score")  # unknown command: the help


def test_the_engine_pass_alerts_and_sends_the_close_instruction(tmp_path: Path) -> None:
    bot, messenger, clock = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 1998")))
    messenger.sent.clear()

    clock.now = FRIDAY_IN_WINDOW  # past the hold and inside the weekend window
    asyncio.run(bot.engine_pass())
    texts = messenger.texts()
    assert any(t.startswith("<b>⏹ CLOSE · cycle 1</b>") and "(closes your" in t for t in texts)
    assert _state(bot) == CycleState.CLOSING
    # the close instruction is sent once, not again on the next pass
    messenger.sent.clear()
    asyncio.run(bot.engine_pass())
    assert not any("CLOSE · cycle" in t for t in messenger.texts())


def test_risk_events_are_forwarded_once(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    asyncio.run(bot.handle(_message("/fill 1 1998")))
    bot.monitor._positions = _empty_positions  # qfex lost its leg
    messenger.sent.clear()
    asyncio.run(bot.engine_pass())
    alerts = [t for t in messenger.texts() if "RISK" in t]
    assert len(alerts) == 1 and "leg_missing → close_cycle" in alerts[0]
    asyncio.run(bot.engine_pass())
    assert len([t for t in messenger.texts() if "RISK" in t]) == 1


async def _empty_positions() -> dict[str, dict[str, Any]]:
    return {"qfex": {}}


# -- the supervisor on Telegram ---------------------------------------------------------------


class FakeSupervisor:
    def __init__(self, proposals: list[Any]) -> None:
        self.questions: list[str] = []
        self._proposals = proposals

    async def ask(self, question: str) -> Any:
        from delta_neutral.supervisor.agent import Run

        self.questions.append(question)
        return Run(question, answer="cycle 1 is open <ok>", proposals=self._proposals, turns=2)


def test_plain_text_goes_to_the_supervisor_and_a_proposal_needs_approval(tmp_path: Path) -> None:
    from delta_neutral.supervisor.tools import Proposal

    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/plan XAU-USD")))
    (_, data), _ = messenger.sent[0][2][0]  # type: ignore[index]
    asyncio.run(bot.handle(_press(data)))
    assert _state(bot) == CycleState.OPEN

    supervisor = FakeSupervisor([Proposal("close_cycle", 1, "test")])
    bot._supervisor = supervisor  # type: ignore[assignment]
    asyncio.run(bot.handle(_message("what is open?")))
    assert supervisor.questions == ["what is open?"]
    answer, card = messenger.sent[-2], messenger.sent[-1]
    assert answer[1].startswith("cycle 1 is open &lt;ok&gt;")  # model text is escaped
    assert card[2] == [
        [("✅ approve", "approve:1:close_cycle"), ("✖ reject", "reject:1:close_cycle")]
    ]
    assert _state(bot) == CycleState.OPEN  # nothing happened yet

    asyncio.run(bot.handle(_press("reject:1:close_cycle")))
    assert _state(bot) == CycleState.OPEN
    asyncio.run(bot.handle(_press("approve:1:close_cycle")))
    assert _state(bot) == CycleState.CLOSING  # the executor ran it, on the operator's tap


def test_ask_without_a_configured_supervisor(tmp_path: Path) -> None:
    bot, messenger, _ = _bot(tmp_path)
    asyncio.run(bot.handle(_message("/ask how much today?")))
    assert messenger.texts() == ["supervisor not configured: see llm.supervisor and .env"]
