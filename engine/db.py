"""Хранилище на sqlite3: позиции, предложения, команды, выплаты, журнал."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import fields
from pathlib import Path

from engine.models import Command, Leg, Position, Proposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, base TEXT NOT NULL, profile TEXT NOT NULL,
    status TEXT NOT NULL, capital REAL NOT NULL, opened_at INTEGER NOT NULL,
    closed_at INTEGER, realized_pnl REAL NOT NULL DEFAULT 0, close_reason TEXT
);
CREATE TABLE IF NOT EXISTS legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    exchange TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL,
    amount REAL NOT NULL, entry_price REAL NOT NULL, leverage REAL NOT NULL, margin REAL NOT NULL,
    fees_paid REAL NOT NULL DEFAULT 0, funding_received REAL NOT NULL DEFAULT 0,
    last_funding_ts INTEGER, pending_rate REAL, pending_ts INTEGER,
    open INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    base TEXT NOT NULL, kind TEXT NOT NULL, profile TEXT NOT NULL, legs_plan TEXT NOT NULL,
    capital REAL NOT NULL, expected_apr REAL NOT NULL, expected_profit REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL, type TEXT NOT NULL, arg TEXT,
    status TEXT NOT NULL DEFAULT 'new', result TEXT
);
CREATE TABLE IF NOT EXISTS funding_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id INTEGER NOT NULL REFERENCES legs(id),
    ts INTEGER NOT NULL, rate REAL NOT NULL, amount REAL NOT NULL,
    UNIQUE(leg_id, ts)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_POS_COLS = ["kind", "base", "profile", "status", "capital", "opened_at",
             "closed_at", "realized_pnl", "close_reason"]
_LEG_COLS = [f.name for f in fields(Leg) if f.name not in ("id", "position_id")]
_PROP_COLS = ["created_at", "expires_at", "base", "kind", "profile", "legs_plan",
              "capital", "expected_apr", "expected_profit", "status"]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Db:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def init(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # --- positions ---
    def _leg_values(self, leg: Leg) -> list:
        return [int(leg.open) if c == "open" else getattr(leg, c) for c in _LEG_COLS]

    def _insert_leg(self, pid: int, leg: Leg) -> None:
        cols = ["position_id"] + _LEG_COLS
        cur = self.conn.execute(
            f"INSERT INTO legs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [pid] + self._leg_values(leg))
        leg.id = cur.lastrowid
        leg.position_id = pid

    def save_position(self, p: Position) -> int:
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO positions ({','.join(_POS_COLS)}) VALUES ({','.join('?' * len(_POS_COLS))})",
                [getattr(p, c) for c in _POS_COLS])
            p.id = cur.lastrowid
            for leg in p.legs:
                self._insert_leg(p.id, leg)
        return p.id

    def update_position(self, p: Position) -> None:
        with self.conn:
            self.conn.execute(
                f"UPDATE positions SET {','.join(c + '=?' for c in _POS_COLS)} WHERE id=?",
                [getattr(p, c) for c in _POS_COLS] + [p.id])
            for leg in p.legs:
                if leg.id is None:
                    self._insert_leg(p.id, leg)
                else:
                    self.conn.execute(
                        f"UPDATE legs SET {','.join(c + '=?' for c in _LEG_COLS)} WHERE id=?",
                        self._leg_values(leg) + [leg.id])

    def _load_legs(self, pid: int) -> list[Leg]:
        rows = self.conn.execute("SELECT * FROM legs WHERE position_id=? ORDER BY id", (pid,))
        legs = []
        for r in rows:
            d = dict(r)
            d["open"] = bool(d["open"])
            legs.append(Leg(**d))
        return legs

    def _position(self, row: sqlite3.Row) -> Position:
        return Position(**dict(row), legs=self._load_legs(row["id"]))

    def get_position(self, position_id: int) -> Position:
        row = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            raise KeyError(position_id)
        return self._position(row)

    def positions(self, status: set[str] | None = None) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall()
        return [self._position(r) for r in rows if status is None or r["status"] in status]

    # --- proposals ---
    def _prop_values(self, p: Proposal) -> list:
        return [json.dumps(p.legs_plan, ensure_ascii=False) if c == "legs_plan" else getattr(p, c)
                for c in _PROP_COLS]

    def save_proposal(self, p: Proposal) -> int:
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO proposals ({','.join(_PROP_COLS)}) VALUES ({','.join('?' * len(_PROP_COLS))})",
                self._prop_values(p))
        p.id = cur.lastrowid
        return p.id

    def update_proposal(self, p: Proposal) -> None:
        with self.conn:
            self.conn.execute(
                f"UPDATE proposals SET {','.join(c + '=?' for c in _PROP_COLS)} WHERE id=?",
                self._prop_values(p) + [p.id])

    @staticmethod
    def _proposal(row: sqlite3.Row) -> Proposal:
        d = dict(row)
        d["legs_plan"] = json.loads(d["legs_plan"])
        return Proposal(**d)

    def get_proposal(self, proposal_id: int) -> Proposal | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return None if row is None else self._proposal(row)

    def pending_proposals(self, now_ms: int) -> list[Proposal]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status='pending' AND expires_at>? ORDER BY id", (now_ms,))
        return [self._proposal(r) for r in rows]

    def expire_proposals(self, now_ms: int) -> list[int]:
        """Перевести просроченные pending-предложения в expired; вернуть их id."""
        with self.conn:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM proposals WHERE status='pending' AND expires_at<=?", (now_ms,))]
            self.conn.executemany(
                "UPDATE proposals SET status='expired' WHERE id=?", [(i,) for i in ids])
        return ids

    # --- commands ---
    def add_command(self, type: str, arg: str | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO commands (created_at, type, arg) VALUES (?,?,?)", (_now_ms(), type, arg))
        return cur.lastrowid

    def take_new_commands(self) -> list[Command]:
        with self.conn:
            rows = self.conn.execute(
                "SELECT * FROM commands WHERE status='new' ORDER BY id").fetchall()
            self.conn.executemany(
                "UPDATE commands SET status='taken' WHERE id=?", [(r["id"],) for r in rows])
        return [Command(**{**dict(r), "status": "taken"}) for r in rows]

    def fail_taken_commands(self, result: str) -> int:
        """Команды, взятые в работу до сбоя/перезапуска, помечаются ошибкой; вернуть их число."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE commands SET status='error', result=? WHERE status='taken'", (result,))
        return cur.rowcount

    def finish_command(self, command_id: int, status: str, result: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE commands SET status=?, result=? WHERE id=?", (status, result, command_id))

    # --- funding ---
    def add_funding(self, leg_id: int, ts: int, rate: float, amount: float) -> bool:
        """True, если выплата записана; False, если (leg_id, ts) уже была."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO funding_payments (leg_id, ts, rate, amount) VALUES (?,?,?,?)",
                (leg_id, ts, rate, amount))
        return cur.rowcount == 1

    def funding_total(self, since_ms: int | None = None) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM funding_payments WHERE ts>=?",
            (since_ms if since_ms is not None else -(2 ** 62),)).fetchone()
        return float(row[0])

    # --- audit / kv ---
    def audit(self, event: str, detail: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (ts, event, detail) VALUES (?,?,?)",
                (_now_ms(), event, json.dumps(detail, ensure_ascii=False)))

    def get_kv(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_kv(self, key: str, value) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    def heartbeat(self, now_ms: int) -> None:
        self.set_kv("heartbeat", now_ms)
