"""Резервные копии базы: снимок, шифрование AES-256-GCM, восстановление."""
from __future__ import annotations

import base64
import binascii
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from engine.db import Db

MAGIC = b"SHIO1"
NONCE_LEN = 12
KEY_ENV = "SHIO_BACKUP_KEY"


class BackupError(Exception):
    """Ошибка резервной копии (текст — на русском, показывается пользователю)."""


def snapshot_bytes(db: Db) -> bytes:
    """Согласованный снимок базы (sqlite3 backup API) без блокировки работающего бота."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snapshot.db")
        dest = sqlite3.connect(path)
        try:
            db.conn.backup(dest)
        finally:
            dest.close()
        return Path(path).read_bytes()


def encrypt(data: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    if len(blob) < len(MAGIC) + NONCE_LEN + 16 or not blob.startswith(MAGIC):
        raise BackupError("Это не резервная копия Shio: файл повреждён или имеет другой формат.")
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE_LEN]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + NONCE_LEN:], MAGIC)
    except InvalidTag:
        raise BackupError("Не удалось расшифровать копию: неверный ключ или файл повреждён.") from None


def load_key() -> bytes | None:
    """Ключ из SHIO_BACKUP_KEY (base64, 32 байта); None, если переменная не задана."""
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise BackupError(f"{KEY_ENV} должен быть в формате base64.") from None
    if len(key) != 32:
        raise BackupError(f"{KEY_ENV} должен содержать ровно 32 байта (сейчас {len(key)}).")
    return key


def restore(blob: bytes, key: bytes, db_path: str) -> str:
    """Заменить базу копией; старый файл остаётся как <db_path>.before-restore-<ts>.

    Возвращает путь старой копии (пустую строку, если базы не было).
    """
    data = decrypt(blob, key)
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".restore-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _check_integrity(tmp_name)
        old = ""
        if target.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            old = f"{db_path}.before-restore-{stamp}"
            n = 1
            while os.path.exists(old):  # второе восстановление в ту же секунду
                n += 1
                old = f"{db_path}.before-restore-{stamp}-{n}"
            os.replace(target, old)
            for suffix in ("-wal", "-shm"):  # хвосты журнала старой базы не должны попасть в новую
                side = Path(db_path + suffix)
                if side.exists():
                    os.replace(side, old + suffix)
        os.replace(tmp_name, target)
        return old
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _check_integrity(path: str) -> None:
    try:
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        result = None
    if not result or result[0] != "ok":
        raise BackupError("Копия расшифрована, но база в ней повреждена. Текущая база не тронута.")
