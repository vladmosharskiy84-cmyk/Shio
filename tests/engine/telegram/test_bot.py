import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.__main__ import _parser, _run, _telegram_supervisor
from engine.config import load_settings
from engine.db import Db
from engine.notifier import LogNotifier
from engine.telegram import bot as tgbot
from engine.telegram.notifier import TelegramNotifier
from tests.engine.telegram.fakes import FakeTelegramApi

ROOT = Path(__file__).resolve().parents[3]
OWNER = 42
NOW = 1_000_000_000_000
TOKEN = "123456:" + "A" * 35


def make_settings(enabled=True, chat_id=OWNER):
    s = load_settings(str(ROOT / "config.yaml"))
    return replace(s, telegram=replace(s.telegram, enabled=enabled, chat_id=chat_id))


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def test_build_notifier_without_token_falls_back(db, monkeypatch, caplog):
    monkeypatch.delenv("SHIO_TELEGRAM_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING):
        notifier, token = tgbot.build_notifier(make_settings(), db)
    assert isinstance(notifier, LogNotifier) and token is None
    assert any("Telegram не настроен" in r.getMessage() and "SHIO_TELEGRAM_TOKEN" in r.getMessage()
               for r in caplog.records)


def test_build_notifier_disabled(db, monkeypatch):
    monkeypatch.setenv("SHIO_TELEGRAM_TOKEN", TOKEN)
    notifier, token = tgbot.build_notifier(make_settings(enabled=False, chat_id=None), db)
    assert isinstance(notifier, LogNotifier) and token is None


def test_build_notifier_with_token(db, monkeypatch):
    monkeypatch.setenv("SHIO_TELEGRAM_TOKEN", TOKEN)
    made = []

    def fake_api(bot):
        made.append(bot)
        return FakeTelegramApi()

    monkeypatch.setattr(tgbot, "AiogramApi", fake_api)
    notifier, token = tgbot.build_notifier(make_settings(), db)
    assert isinstance(notifier, TelegramNotifier) and token == TOKEN
    assert notifier.chat_id == OWNER and len(made) == 1


def test_build_notifier_bad_token_falls_back(db, monkeypatch, caplog):
    monkeypatch.setenv("SHIO_TELEGRAM_TOKEN", "не токен")
    with caplog.at_level(logging.WARNING):
        notifier, token = tgbot.build_notifier(make_settings(), db)
    assert isinstance(notifier, LogNotifier) and token is None
    assert any("Telegram не настроен" in r.getMessage() for r in caplog.records)


def test_route_message_replies_to_owner_and_ignores_foreign(db):
    api = FakeTelegramApi()
    s = make_settings()
    asyncio.run(tgbot.route_message(api, "/help", OWNER, OWNER, db, s, NOW))
    assert len(api.messages) == 1 and api.messages[0]["chat_id"] == OWNER
    asyncio.run(tgbot.route_message(api, "/help", 999, 999, db, s, NOW))
    assert len(api.messages) == 1  # чужому не отвечаем


def test_route_message_passes_buttons(db):
    api = FakeTelegramApi()
    asyncio.run(tgbot.route_message(api, "/stop", OWNER, OWNER, db, make_settings(), NOW))
    assert api.messages[0]["buttons"]  # кнопки подтверждения доходят до API


def test_route_callback_answers_and_removes_buttons(db):
    api = FakeTelegramApi()
    asyncio.run(tgbot.route_callback(api, "cb1", "cancel", OWNER, OWNER, 7, "Закрыть позицию 1?",
                                     db, make_settings(), NOW))
    assert [a[0] for a in api.answers] == ["cb1"]
    assert len(api.edits) == 1 and api.edits[0]["message_id"] == 7
    assert api.edits[0]["text"].startswith("Закрыть позицию 1?")
    assert "Отменено." in api.edits[0]["text"]
    assert api.messages == []


def test_route_callback_edit_failure_sends_new_message(db):
    class EditFails(FakeTelegramApi):
        async def edit_message(self, chat_id, message_id, text):
            raise ConnectionError("нет сети")

    api = EditFails()
    asyncio.run(tgbot.route_callback(api, "cb1", "cancel", OWNER, OWNER, 7, "x", db, make_settings(), NOW))
    assert len(api.messages) == 1 and "Отменено." in api.messages[0]["text"]


def test_route_callback_foreign_is_silent(db):
    api = FakeTelegramApi()
    asyncio.run(tgbot.route_callback(api, "cb1", "stop:confirm", 999, 999, 7, "x", db,
                                     make_settings(), NOW))
    assert api.edits == [] and api.messages == []
    assert all(text == "" for _, text in api.answers)
    assert db.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0


def test_chat_id_from_update():
    msg = SimpleNamespace(chat=SimpleNamespace(id=5))
    assert tgbot.chat_id_from_update(msg) == 5
    assert tgbot.chat_id_from_update(SimpleNamespace(message=msg, callback_query=None)) == 5
    cb = SimpleNamespace(message=msg, from_user=SimpleNamespace(id=9))
    assert tgbot.chat_id_from_update(cb) == 5
    cb_no_msg = SimpleNamespace(message=None, from_user=SimpleNamespace(id=9))
    assert tgbot.chat_id_from_update(cb_no_msg) == 9
    assert tgbot.chat_id_from_update({"message": {"chat": {"id": 7}}}) == 7
    assert tgbot.chat_id_from_update({"callback_query": {"message": {"chat": {"id": 8}}}}) == 8
    assert tgbot.chat_id_from_update({"update_id": 1}) is None
    assert tgbot.chat_id_from_update(SimpleNamespace(message=None, callback_query=None)) is None


def test_cli_has_telegram_id_command():
    args = _parser().parse_args(["telegram-id"])
    assert args.cmd == "telegram-id" and callable(args.fn)


def test_supervisor_restarts_after_failure_and_logs(caplog):
    calls = {"n": 0}

    async def flaky(stop_event):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("упало")
        stop_event.set()

    async def go():
        await _telegram_supervisor(flaky, asyncio.Event(), backoff_s=0.01)

    with caplog.at_level(logging.ERROR):
        asyncio.run(go())
    assert calls["n"] == 3
    assert any("Telegram" in r.getMessage() for r in caplog.records)


def test_telegram_task_failure_does_not_stop_runner(tmp_path, monkeypatch, caplog):
    from tests.engine.test_runner import make_cfg
    settings = load_settings(make_cfg(tmp_path))
    ran = {"runner": False, "tg": 0}

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run_forever(self):
            await asyncio.sleep(0.15)
            ran["runner"] = True

        def stop(self):
            pass

    async def failing_tg(token, s, db, notifier, stop):
        ran["tg"] += 1
        raise ConnectionError("telegram упал")

    monkeypatch.setattr("engine.runner.Runner", FakeRunner)
    monkeypatch.setattr(tgbot, "run_telegram", failing_tg)
    monkeypatch.setattr(tgbot, "build_notifier", lambda s, db: (LogNotifier(db), TOKEN))
    monkeypatch.setattr("engine.__main__.TELEGRAM_BACKOFF_S", 0.01)
    with caplog.at_level(logging.ERROR):
        asyncio.run(_run(settings))
    assert ran["runner"] and ran["tg"] >= 2
    assert any("telegram упал" in r.getMessage() or "Telegram" in r.getMessage()
               for r in caplog.records)


def test_route_callback_survives_answer_failure(db):
    class AnswerFails(FakeTelegramApi):
        async def answer_callback(self, callback_id, text):
            raise RuntimeError("query is too old")

    api = AnswerFails()
    asyncio.run(tgbot.route_callback(api, "cb1", "cancel", OWNER, OWNER, 7, "x", db, make_settings(), NOW))
    assert len(api.edits) == 1 and "Отменено." in api.edits[0]["text"]


def test_discovery_dispatcher_captures_chat_id_from_real_update():
    from datetime import datetime, timezone

    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    async def go(update):
        found: list[int] = []
        stop = asyncio.Event()
        dp = tgbot._build_discovery_dispatcher(found, stop)
        await dp.feed_update(Bot(TOKEN), update)
        return found, stop.is_set()

    now = datetime.now(tz=timezone.utc)
    msg = Message(message_id=1, date=now, chat=Chat(id=77, type="private"), text="hi")
    found, stopped = asyncio.run(go(Update(update_id=1, message=msg)))
    assert [f.chat_id for f in found] == [77] and stopped
    assert found[0].name is None and found[0].username is None
    user = User(id=88, is_bot=False, first_name="Иван", last_name="Петров", username="ivan")
    cb = CallbackQuery(id="c", from_user=user, chat_instance="i", data="d", message=msg)
    found, _ = asyncio.run(go(Update(update_id=2, callback_query=cb)))
    assert found == [tgbot.Sender(77, 88, "Иван Петров", "ivan", "private")]


def test_route_ignores_non_owner_sender_in_owner_chat(db):
    api = FakeTelegramApi()
    asyncio.run(tgbot.route_message(api, "/pause", OWNER, 999, db, make_settings(), NOW))
    asyncio.run(tgbot.route_callback(api, "cb1", "stop:confirm", OWNER, 999, 7, "x", db,
                                     make_settings(), NOW))
    assert api.messages == [] and api.edits == []
    assert all(text == "" for _, text in api.answers)
    assert db.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0


def test_dispatcher_passes_sender_id(db, monkeypatch):
    from datetime import datetime, timezone

    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    seen = []

    async def fake_route_message(api, text, chat_id, user_id, *a):
        seen.append(("msg", chat_id, user_id))

    async def fake_route_callback(api, cb_id, data, chat_id, user_id, *a):
        seen.append(("cb", chat_id, user_id))

    monkeypatch.setattr(tgbot, "route_message", fake_route_message)
    monkeypatch.setattr(tgbot, "route_callback", fake_route_callback)
    now = datetime.now(tz=timezone.utc)
    user = User(id=5, is_bot=False, first_name="x")
    msg = Message(message_id=1, date=now, chat=Chat(id=OWNER, type="private"), text="/help",
                  from_user=user)
    cb = CallbackQuery(id="c", from_user=user, chat_instance="i", data="cancel", message=msg)

    async def go():
        dp = tgbot._build_dispatcher(FakeTelegramApi(), make_settings(), db)
        bot = Bot(TOKEN)
        await dp.feed_update(bot, Update(update_id=1, message=msg))
        await dp.feed_update(bot, Update(update_id=2, callback_query=cb))
        await bot.session.close()

    asyncio.run(go())
    assert seen == [("msg", OWNER, 5), ("cb", OWNER, 5)]


@pytest.mark.parametrize("sender,expect,group_warning", [
    (tgbot.Sender(42, 42, "Иван Петров", "ivan", "private"),
     ["Ваш chat_id: 42", "Иван Петров", "@ivan"], False),
    (tgbot.Sender(-100500, 42, "Иван", None, "supergroup"), ["-100500", "Иван"], True),
])
def test_telegram_id_prints_sender(monkeypatch, capsys, tmp_path, sender, expect, group_warning):
    from tests.engine.test_runner import make_cfg

    async def fake_discover(token, timeout_s=120.0):
        return sender

    monkeypatch.setenv("SHIO_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(tgbot, "discover_chat_id", fake_discover)
    from engine.__main__ import main
    code = main(["--config", make_cfg(tmp_path), "telegram-id"])
    out = capsys.readouterr().out
    for piece in expect:
        assert piece in out
    assert ("не из личного чата" in out) == group_warning
    assert code == (1 if group_warning else 0)
