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


MIN_PAIR_CAPITAL = 20.0      # меньше — связку не открываем
BALANCE_RESERVE = 0.98       # 2% свободного баланса оставляем на комиссии и округления
NO_BALANCE_REASON = "недостаточно свободного баланса"


def capital_for_pair(equity: float, settings: Settings) -> float:
    return equity * settings.limits.max_pair_share


def affordable_capital(kind: str, legs_plan: list[dict], capital: float,
                       balances: dict[str, float]) -> float:
    """Капитал связки, урезанный до того, что позволяют свободные балансы её бирж.

    spot-perp на бирже X: весь капитал на X → не больше free(X)×0.98.
    perp-perp: на каждой бирже нужна половина капитала → не больше 2×min(free)×0.98.
    """
    exchanges = sorted({leg["exchange"] for leg in legs_plan})
    free = min((balances.get(ex, 0.0) for ex in exchanges), default=0.0)
    limit = free * BALANCE_RESERVE * (2 if kind == "perp-perp" else 1)
    return max(0.0, min(capital, limit))


def reserve_capital(kind: str, legs_plan: list[dict], capital: float,
                    balances: dict[str, float]) -> None:
    """Вычесть капитал принятой связки из балансов (spot-perp — весь с X, perp-perp — по половине)."""
    exchanges = sorted({leg["exchange"] for leg in legs_plan})
    share = capital / 2 if kind == "perp-perp" else capital
    for ex in exchanges:
        balances[ex] = balances.get(ex, 0.0) - share


def check_limits(base: str, profile: str, capital: float, open_positions: list[Position],
                 active_proposals: list[Proposal], equity: float, day_pnl: float, paused: bool,
                 settings: Settings) -> str | None:
    """Причина, по которой связку сейчас открывать нельзя (по-русски), или None.

    Учитываются позиции opening/open/closing и предложения pending/approved.
    """
    lim = settings.limits
    held = [p for p in open_positions if p.status in _OPEN_STATUSES]
    active = [p for p in active_proposals if p.status in _ACTIVE_PROPOSALS]
    if paused:
        return "бот на паузе"
    if day_pnl <= -lim.daily_loss_pause * equity:
        return "дневной убыток достиг лимита, новые связки не открываем"
    if len(held) + len(active) >= lim.max_positions:
        return f"уже открыто максимум связок ({lim.max_positions})"
    if base in {p.base for p in held} | {p.base for p in active}:
        return f"монета {base} уже в позиции или в предложении"
    aggressive = sum(p.capital for p in [*held, *active] if p.profile == "aggressive")
    if profile == "aggressive" and aggressive + capital > lim.aggressive_share * equity:
        return "превышена доля агрессивных связок"
    return None


def decide(candidates: list[Candidate], open_positions: list[Position], pending: list[Proposal],
           equity: float, day_pnl: float, paused: bool, settings: Settings) -> list[Decision]:
    accepted: list[Proposal] = []  # решения этого же вызова занимают слоты, как предложения
    out: list[Decision] = []
    for c in candidates:
        reason = check_limits(c.op.base, c.profile, c.capital, open_positions,
                              [*pending, *accepted], equity, day_pnl, paused, settings)
        if reason is None and not c.liquidity_ok:
            reason = "недостаточно ликвидности"
        if reason is None and c.op.apr_net_capital <= 0:
            reason = "доходность не положительная"
        if reason is not None:
            out.append(Decision("skip", c, reason))
            continue
        accepted.append(Proposal(None, 0, 0, c.op.base, c.op.kind, c.profile, c.legs_plan,
                                 c.capital, c.op.apr_net_capital, c.op.profit_hold_usd, "pending"))
        if settings.mode == "auto" and c.op.apr_net_capital >= settings.limits.min_auto_apr:
            out.append(Decision("enter", c, "автомат: доходность выше порога"))
        else:
            out.append(Decision("propose", c, "предложить на подтверждение"))
    return out
