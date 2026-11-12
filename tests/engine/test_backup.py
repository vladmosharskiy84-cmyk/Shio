import asyncio
import base64
import logging
import os
import sqlite3
import time

import pytest

from engine import backup
from engine.__main__ import main
from engine.backup import BackupError
from engine.db import Db
from engine.notifier import LogNotifier
from tests.engine.test_db import make_position
from tests.engine.test_runner import make_cfg

KEY = bytes(range(32))


def run(coro):
    return asyncio.run(coro)


def b64(key=KEY):
    return base64.b64encode(key).decode()


def test_encrypt_decrypt_roundtrip():
    blob = backup.encrypt(b"hello shio", KEY)
    assert blob.startswith(b"SHIO1") and len(blob) == 5 + 12 + len(b"hello shio") + 16
    assert backup.decrypt(blob, KEY) == b"hello shio"
    assert backup.encrypt(b"hello shio", KEY) != blob  # свой nonce каждый раз


def test_wrong_key_raises_backup_error():
    blob = backup.encrypt(b"data", KEY)
    with pytest.raises(BackupError) as e:
        backup.decrypt(blob, bytes(32))
    assert "ключ" in str(e.value)


def test_tampered_blob_raises():
    blob = bytearray(backup.encrypt(b"data", KEY))
    blob[-1] ^= 1
    with pytest.raises(BackupError):
        backup.decrypt(bytes(blob), KEY)
    with pytest.raises(BackupError):
        backup.decrypt(b"XXXXX" + bytes(blob[5:]), KEY)
    with pytest.raises(BackupError):
        backup.decrypt(b"SHIO1", KEY)


def test_load_key_validates_length(monkeypatch):
    monkeypatch.delenv("SHIO_BACKUP_KEY", raising=False)
    assert backup.load_key() is None
    monkeypatch.setenv("SHIO_BACKUP_KEY", b64())
    assert backup.load_key() == KEY
    monkeypatch.setenv("SHIO_BACKUP_KEY", b64(b"short"))
    with pytest.raises(BackupError):
        backup.load_key()
    monkeypatch.setenv("SHIO_BACKUP_KEY", "не base64!!")
    with pytest.raises(BackupError):
        backup.load_key()


def test_restore_replaces_db_and_keeps_old(tmp_path):
    db = Db(str(tmp_path / "shio.db"))
    db.init()
    db.set_kv("marker", "from-backup")
    pid = db.save_position(make_position())
    blob = backup.encrypt(backup.snapshot_bytes(db), KEY)
    db.set_kv("marker", "current")
    changed = db.get_position(pid)
    changed.status = "closed"
    db.update_position(changed)
    db.conn.close()
    old = backup.restore(blob, KEY, str(tmp_path / "shio.db"))
    assert os.path.exists(old) and ".before-restore-" in old
    new = Db(str(tmp_path / "shio.db"))
    assert new.get_kv("marker") == "from-backup"
    restored = new.get_position(pid)
    assert restored.status == "open" and restored.base == "BTC" and len(restored.legs) == 2
    new.conn.close()
    prev = sqlite3.connect(old)
    assert prev.execute("SELECT value FROM kv WHERE key='marker'").fetchone()[0] == '"current"'
    prev.close()


def test_restore_bad_key_changes_nothing(tmp_path):
    db = Db(str(tmp_path / "shio.db"))
    db.init()
    blob = backup.encrypt(backup.snapshot_bytes(db), KEY)
    db.conn.close()
    with pytest.raises(BackupError):
        backup.restore(blob, bytes(32), str(tmp_path / "shio.db"))
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("shio.db")) == ["shio.db"]


def test_restore_rejects_non_database(tmp_path):
    (tmp_path / "shio.db").write_bytes(b"orig")
    blob = backup.encrypt(b"this is not sqlite" * 50, KEY)
    with pytest.raises(BackupError):
        backup.restore(blob, KEY, str(tmp_path / "shio.db"))
    assert (tmp_path / "shio.db").read_bytes() == b"orig"


def test_log_notifier_saves_backup_file(tmp_path, monkeypatch, caplog):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    db = Db(":memory:")
    db.init()
    folder = tmp_path / "data" / "backups"
    with caplog.at_level(logging.INFO):
        ok = run(LogNotifier(db, folder).send_document("shio-2026-01-01.db.enc", b"abc", "копия"))
    saved = folder / "shio-2026-01-01.db.enc"
    assert ok is True and saved.read_bytes() == b"abc"
    assert str(saved) in caplog.text
    assert list(cwd.iterdir()) == []  # в текущей папке ничего не создано


def test_restore_refuses_while_bot_alive(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("SHIO_BACKUP_KEY", b64())
    db = Db(str(tmp_path / "shio.db"))
    db.init()
    blob = backup.encrypt(backup.snapshot_bytes(db), KEY)
    db.heartbeat(int(time.time() * 1000))
    db.conn.close()
    f = tmp_path / "b.enc"
    f.write_bytes(blob)
    assert main(["--config", cfg, "restore", str(f)]) == 1
    assert "запущен" in capsys.readouterr().out


def test_restore_command_when_stopped(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("SHIO_BACKUP_KEY", b64())
    db = Db(str(tmp_path / "shio.db"))
    db.init()
    db.set_kv("marker", 1)
    blob = backup.encrypt(backup.snapshot_bytes(db), KEY)
    db.set_kv("marker", 2)
    db.heartbeat(int(time.time() * 1000) - 10 * 60_000)
    db.conn.close()
    f = tmp_path / "b.enc"
    f.write_bytes(blob)
    assert main(["--config", cfg, "restore", str(f)]) == 0
    assert ".before-restore-" in capsys.readouterr().out
    db = Db(str(tmp_path / "shio.db"))
    assert db.get_kv("marker") == 1
    db.conn.close()


def test_two_restores_in_same_second_keep_both_old_files(tmp_path):
    path = str(tmp_path / "shio.db")
    db = Db(path)
    db.init()
    db.set_kv("m", 1)
    blob = backup.encrypt(backup.snapshot_bytes(db), KEY)
    db.conn.close()
    first = backup.restore(blob, KEY, path)
    second = backup.restore(blob, KEY, path)
    assert first != second and os.path.exists(first) and os.path.exists(second)


def test_restore_without_existing_db_creates_no_empty_one_and_no_fake_old_path(
        tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("SHIO_BACKUP_KEY", b64())
    src = Db(str(tmp_path / "src.db"))
    src.init()
    f = tmp_path / "b.enc"
    f.write_bytes(backup.encrypt(backup.snapshot_bytes(src), KEY))
    src.conn.close()
    missing = tmp_path / "nofile.enc"
    assert main(["--config", cfg, "restore", str(missing)]) == 1
    assert not (tmp_path / "shio.db").exists()  # пустая база не создана
    capsys.readouterr()
    assert main(["--config", cfg, "restore", str(f)]) == 0
    out = capsys.readouterr().out
    assert "before-restore" not in out and (tmp_path / "shio.db").exists()
