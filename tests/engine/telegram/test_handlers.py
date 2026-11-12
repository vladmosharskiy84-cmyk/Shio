from dataclasses import replace
from pathlib import Path

import pytest

from engine.config import load_settings
from engine.db import Db
from engine.models import Leg, Position, Proposal
from engine.telegram.api import Button
from engine.telegram.handlers import handle_callback, handle_message

ROOT = Path(__file__).resolve().parents[3]
OWNER = 42
NOW = 1_000_000_000_000
HOUR = 3_600_000


@pytest.fixture
def settings():
    s = load_settings(str(ROOT / "config.yaml"))
    return replace(s, telegram=replace(s.telegram, chat_id=OWNER))


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def cmds(db):
    return [(r["type"], r["arg"]) for r in db.conn.execute("SELECT type, arg FROM commands ORDER BY id")]


def foreign_events(db):
    return db.conn.execute("SELECT detail FROM audit_log WHERE event='telegram_foreign'").fetchall()


def add_proposal(db, expires=NOW + 900_000, status="pending"):
    return db.save_proposal(Proposal(None, NOW, expires, "ETH", "spot-perp", "calm", [],
                                     100.0, 50.0, 1.0, status))


def add_position(db, status="open"):
    perp = Leg(None, None, "okx", "SOL/USDT:USDT", "perp", "short", 1.0, 100.0, 3.0, 33.0)
    spot = Leg(None, None, "bybit", "SOL/USDT", "spot", "long", 1.0, 100.0, 1.0, 100.0)
    return db.save_position(Position(None, "spot-perp", "SOL", "calm", status, 200.0, 0,
                                     legs=[spot, perp]))


def test_foreign_chat_ignored_and_audited_once_per_hour(db, settings):
    assert handle_message("/pause", 7, db, settings, NOW) is None
    assert handle_callback("stop:confirm", 7, db, settings, NOW + 1000) is None
    assert cmds(db) == []
    assert len(foreign_events(db)) == 1
    handle_message("/pause", 7, db, settings, NOW + HOUR + 1)
    assert len(foreign_events(db)) == 2
    handle_message("/pause", 8, db, settings, NOW + HOUR + 2)
    assert len(foreign_events(db)) == 3


def test_unconfigured_telegram_treats_everyone_as_foreign(db, settings):
    s = replace(settings, telegram=replace(settings.telegram, chat_id=None))
    assert handle_message("/pause", OWNER, db, s, NOW) is None
    assert cmds(db) == []


def test_status_and_positions_texts(db, settings):
    add_position(db)
    assert "SOL" in handle_message("/positions", OWNER, db, settings, NOW).text
    assert handle_message("/status", OWNER, db, settings, NOW).text
    assert "/stop" in handle_message("/help", OWNER, db, settings, NOW).text
    assert "/pause" in handle_message("/start", OWNER, db, settings, NOW).text


def test_pause_resume_write_commands(db, settings):
    r = handle_message("/pause", OWNER, db, settings, NOW)
    assert "Принято" in r.text
    handle_message("/resume", OWNER, db, settings, NOW)
    assert cmds(db) == [("pause", None), ("resume", None)]


def test_stop_requires_confirmation(db, settings):
    r = handle_message("/stop", OWNER, db, settings, NOW)
    assert r.buttons == [[Button("Да, закрыть всё", "stop:confirm"), Button("Отмена", "cancel")]]
    assert cmds(db) == []
    handle_callback("stop:confirm", OWNER, db, settings, NOW)
    handle_callback("stop:confirm", OWNER, db, settings, NOW + 5000)
    assert cmds(db) == [("stop_all", None)]
    handle_callback("stop:confirm", OWNER, db, settings, NOW + 61_000)
    assert cmds(db) == [("stop_all", None), ("stop_all", None)]


def test_cancel(db, settings):
    assert "Отменено" in handle_callback("cancel", OWNER, db, settings, NOW).text
    assert cmds(db) == []


def test_close_requires_confirmation(db, settings):
    pid = add_position(db)
    r = handle_message(f"/close {pid}", OWNER, db, settings, NOW)
    assert r.buttons == [[Button("Да, закрыть", f"close:confirm:{pid}"), Button("Отмена", "cancel")]]
    assert cmds(db) == []
    handle_callback(f"close:confirm:{pid}", OWNER, db, settings, NOW)
    handle_callback(f"close:confirm:{pid}", OWNER, db, settings, NOW + 1000)
    assert cmds(db) == [("close", str(pid))]


def test_close_unknown_position_rejected(db, settings):
    closed = add_position(db, "closed")
    for text in ("/close 999", f"/close {closed}", "/close", "/close abc"):
        r = handle_message(text, OWNER, db, settings, NOW)
        assert r.buttons is None
    assert cmds(db) == []


def test_approve_double_tap_writes_one_command(db, settings):
    pid = add_proposal(db)
    assert "Принято" in handle_callback(f"approve:{pid}", OWNER, db, settings, NOW).text
    r = handle_callback(f"approve:{pid}", OWNER, db, settings, NOW + 1000)
    assert "уже обработано" in r.text
    assert cmds(db) == [("approve", str(pid))]


def test_approve_after_expiry_rejected(db, settings):
    pid = add_proposal(db, expires=NOW + 1000)
    r = handle_callback(f"approve:{pid}", OWNER, db, settings, NOW + 1000)
    assert "истекло" in r.text
    assert "не найдено" in handle_callback("approve:999", OWNER, db, settings, NOW).text
    assert "не найдено" in handle_callback("approve:x", OWNER, db, settings, NOW).text
    assert cmds(db) == []


def test_approve_non_pending_is_already_handled(db, settings):
    pid = add_proposal(db, status="executed")
    assert "уже обработано" in handle_callback(f"approve:{pid}", OWNER, db, settings, NOW).text
    assert cmds(db) == []


def test_reject_writes_reject(db, settings):
    pid = add_proposal(db)
    assert "Пропущено" in handle_callback(f"reject:{pid}", OWNER, db, settings, NOW).text
    assert handle_callback(f"approve:{pid}", OWNER, db, settings, NOW).text.rstrip(".").endswith("обработано")
    assert cmds(db) == [("reject", str(pid))]


def test_unknown_command_hints_help(db, settings):
    assert "/help" in handle_message("/foo", OWNER, db, settings, NOW).text
    assert "/help" in handle_message("привет", OWNER, db, settings, NOW).text
    assert "/help" in handle_callback("zzz", OWNER, db, settings, NOW).text
