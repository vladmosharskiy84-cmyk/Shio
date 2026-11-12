import subprocess
import sys
from pathlib import Path

from engine.config import load_settings
from engine.db import Db
from engine.guardian import leg_buffer
from engine.models import Leg, Position, Proposal
from engine.report import build_status, format_positions, format_status, split_message

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_000_000_000_000


def test_split_message_respects_limit():
    text = "\n".join("x" * 99 for _ in range(101))  # ~10 000 символов
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert "\n".join(parts) == text


def test_split_message_cuts_overlong_line():
    parts = split_message("y" * 9000)
    assert len(parts) == 3 and all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == "y" * 9000
    assert split_message("коротко") == ["коротко"]


def test_format_positions_lists_buffers():
    s = load_settings(str(ROOT / "config.yaml"))
    db = Db(":memory:")
    db.init()
    perp = Leg(None, None, "okx", "SOL/USDT:USDT", "perp", "short", 1.0, 100.0, 3.0, 100.0 / 3)
    spot = Leg(None, None, "bybit", "SOL/USDT", "spot", "long", 1.0, 100.0, 1.0, 100.0,
               funding_received=0.0)
    perp.funding_received = 1.25
    db.save_position(Position(None, "spot-perp", "SOL", "calm", "open", 200.0, 0,
                              realized_pnl=-0.5, legs=[spot, perp]))
    db.set_kv("stats", {"marks": {"okx|SOL/USDT:USDT": 120.0}})
    text = format_positions(db, s, NOW)
    assert "SOL" in text and "запас" in text and "+1.25" in text and "-0.50" in text
    assert f"{leg_buffer(perp, 120.0):.0%}" in text


def test_format_positions_empty():
    s = load_settings(str(ROOT / "config.yaml"))
    db = Db(":memory:")
    db.init()
    assert "нет" in format_positions(db, s, NOW)


def test_cli_status_unchanged(tmp_path):
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8").replace(
        "db_path: data/shio.db", f"db_path: {tmp_path / 'x.db'}")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(cfg, encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "engine", "--config", str(cfg_path), "status"],
                         capture_output=True, text=True, cwd=ROOT, check=True).stdout
    expected = (
        "Режим: Полуавтомат (каждую связку подтверждаете вы)\n"
        "Торговля: виртуальная (бумажная)\n"
        "Пауза: нет\n"
        "Бот: не запущен\n"
        "Капитал: 500.00 USD (стартовый 500.00 USD), по ценам входа\n"
        "P&L за день: +0.00 USD\n"
        "P&L всего: +0.00 USD\n"
        "Получено funding всего: +0.00 USD\n"
        "Открытых связок нет\n"
        "Предложений ждут подтверждения: 0\n")
    assert out == expected  # текст зафиксирован до переноса функций
    s = load_settings(str(cfg_path))
    db = Db(str(tmp_path / "x.db"))
    db.init()
    assert format_status(build_status(db, s, NOW)) + "\n" == out


def test_split_message_never_emits_empty_parts():
    assert split_message("") == []
    assert split_message("\n\n") == []
    parts = split_message("a" * 4096 + "\n")
    assert parts == ["a" * 4096]
    parts = split_message("b" * 4096 + "\n\n" + "c")
    assert all(p.strip() for p in parts) and "".join(parts).replace("\n", "") == "b" * 4096 + "c"


def test_format_status_cli_hint_only_for_cli():
    s = load_settings(str(ROOT / "config.yaml"))
    db = Db(":memory:")
    db.init()
    db.save_proposal(Proposal(None, NOW, NOW + 900_000, "ETH", "spot-perp", "calm", [],
                              100.0, 50.0, 1.0, "pending"))
    st = build_status(db, s, NOW)
    assert "(python -m engine proposals)" in format_status(st)
    assert "python -m engine" not in format_status(st, cli_hint=False)
