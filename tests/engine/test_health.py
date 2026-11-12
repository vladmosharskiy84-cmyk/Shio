import sqlite3
import time
from pathlib import Path

from engine.__main__ import main
from engine.config import load_settings
from engine.db import Db
from tests.engine.test_runner import make_cfg


def _now_ms():
    return int(time.time() * 1000)


def _db(cfg):
    db = Db(load_settings(cfg).db_path)
    db.init()
    return db


def test_health_fresh(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    db = _db(cfg)
    db.heartbeat(_now_ms() - 30_000)
    db.conn.close()
    assert main(["--config", cfg, "health"]) == 0


def test_health_stale(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    db = _db(cfg)
    db.heartbeat(_now_ms() - 300_000)
    db.conn.close()
    assert main(["--config", cfg, "health"]) == 1
    out = capsys.readouterr().out.strip()
    assert out.startswith("Движок молчит 30") and out.endswith(" с")
    assert len(out.splitlines()) == 1


def test_health_no_heartbeat_yet(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    _db(cfg).conn.close()
    assert main(["--config", cfg, "health"]) == 1
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1 and "сигнал" in out


def test_health_no_db(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    path = load_settings(cfg).db_path
    assert main(["--config", cfg, "health"]) == 1
    assert capsys.readouterr().out.strip() == f"База не найдена: {path}"
    assert not Path(path).exists()  # проверка не создаёт пустую базу


def test_health_foreign_db(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    path = load_settings(cfg).db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    assert main(["--config", cfg, "health"]) == 1
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1 and "База" in out


def test_health_bad_config_exit_1(tmp_path, capsys):
    # Docker понимает только 0 (здоров) и 1 (болен): ошибка настроек — тоже 1
    assert main(["--config", str(tmp_path / "missing.yaml"), "health"]) == 1
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1 and "настройках" in out
