"""Модели данных движка."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PositionStatus = Literal["opening", "open", "closing", "closed", "failed", "liquidated"]


@dataclass
class Leg:
    id: int | None
    position_id: int | None
    exchange: str
    symbol: str
    kind: Literal["spot", "perp"]
    side: Literal["long", "short"]
    amount: float
    entry_price: float
    leverage: float
    margin: float
    fees_paid: float = 0.0
    funding_received: float = 0.0
    last_funding_ts: int | None = None
    pending_rate: float | None = None
    pending_ts: int | None = None
    open: bool = True


@dataclass
class Position:
    id: int | None
    kind: Literal["spot-perp", "perp-perp"]
    base: str
    profile: Literal["calm", "aggressive"]
    status: PositionStatus
    capital: float
    opened_at: int
    closed_at: int | None = None
    realized_pnl: float = 0.0
    close_reason: str | None = None
    legs: list[Leg] = field(default_factory=list)


@dataclass
class Proposal:
    id: int | None
    created_at: int
    expires_at: int
    base: str
    kind: str
    profile: str
    legs_plan: list[dict]
    capital: float
    expected_apr: float
    expected_profit: float
    status: Literal["pending", "approved", "rejected", "expired", "executed", "failed"]


@dataclass
class Command:
    id: int | None
    created_at: int
    type: Literal["approve", "reject", "close", "stop_all", "pause", "resume"]
    arg: str | None
    status: Literal["new", "taken", "done", "error"] = "new"
    result: str | None = None
