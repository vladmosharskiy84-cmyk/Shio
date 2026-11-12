"""Решатель: из ранжированных кандидатов — предложить, войти или пропустить, с учётом лимитов."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import Settings
from .finder import Candidate
from .models import Position, Proposal

_OPEN_STATUSES = {"opening", "open", "closing"}
_ACTIVE_PROPOSALS = {"pending", "approved"}


@dataclass
class Decision:
    action: Literal["propose", "enter", "skip"]
    candidate: Candidate
    reason: str


def capital_for_pair(equity: float, settings: Settings) -> float:
    return equity * settings.limits.max_pair_share


def decide(candidates: list[Candidate], open_positions: list[Position], pending: list[Proposal],
           equity: float, day_pnl: float, paused: bool, settings: Settings) -> list[Decision]:
    lim = settings.limits
    held = [p for p in open_positions if p.status in _OPEN_STATUSES]
    active = [p for p in pending if p.status in _ACTIVE_PROPOSALS]
    bases = {p.base for p in held} | {p.base for p in active}
    slots = lim.max_positions - len(held) - len(active)
    aggressive = sum(p.capital for p in [*held, *active] if p.profile == "aggressive")
    day_loss_hit = day_pnl <= -lim.daily_loss_pause * equity

    out: list[Decision] = []
    for c in candidates:
        def skip(reason: str) -> None:
            out.append(Decision("skip", c, reason))

        if paused:
            skip("бот на паузе")
        elif day_loss_hit:
            skip("дневной убыток достиг лимита, новые связки не открываем")
        elif slots <= 0:
            skip(f"уже открыто максимум связок ({lim.max_positions})")
        elif c.op.base in bases:
            skip(f"монета {c.op.base} уже в позиции или в предложении")
        elif not c.liquidity_ok:
            skip("недостаточно ликвидности")
        elif (c.profile == "aggressive"
              and aggressive + c.capital > lim.aggressive_share * equity):
            skip("превышена доля агрессивных связок")
        elif c.op.apr_net_capital <= 0:
            skip("доходность не положительная")
        else:
            slots -= 1
            bases.add(c.op.base)
            if c.profile == "aggressive":
                aggressive += c.capital
            if settings.mode == "auto" and c.op.apr_net_capital >= lim.min_auto_apr:
                out.append(Decision("enter", c, "автомат: доходность выше порога"))
            else:
                out.append(Decision("propose", c, "предложить на подтверждение"))
    return out
