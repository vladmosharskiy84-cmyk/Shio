"""Тексты отчётов, общие для командной строки и Telegram-бота."""
from __future__ import annotations

from datetime import datetime, timezone

from engine import ledger
from engine.collector import MarketSnapshot
from engine.config import Settings
from engine.db import Db
from engine.guardian import leg_buffer

_HELD = {"opening", "open", "closing"}
_ALIVE_MS = 3 * 60_000  # сигнал «жив» не старше 3 минут — бот работает
_POS_STATUS_RU = {"opening": "открывается", "open": "открыта", "closing": "закрывается"}


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_status(db: Db, settings: Settings, now_ms: int) -> dict:
    """Сводка для `status`; только читает базу (последние цифры пишет работающий бот)."""
    stats = db.get_kv("stats") or {}
    positions = db.positions(_HELD)
    if "equity" in stats:
        equity, day_pnl, updated = stats["equity"], stats.get("day_pnl", 0.0), stats.get("ts")
    else:  # бот ещё не работал: балансы + позиции по цене входа
        balances = db.get_kv("paper_balances") or dict(settings.paper_balances)
        equity = ledger.equity(db, balances, positions, MarketSnapshot(ts=now_ms))
        day_pnl, updated = 0.0, None
    heartbeat = db.get_kv("heartbeat")
    buffers = stats.get("buffers") or {}
    return {
        "mode": settings.mode,
        "virtual": settings.virtual,
        "paused": bool(db.get_kv("paused", False)),
        "alive": heartbeat is not None and now_ms - heartbeat <= _ALIVE_MS,
        "heartbeat": heartbeat,
        "capital": settings.capital_usd,
        "equity": equity,
        "day_pnl": day_pnl,
        "pnl_total": equity - settings.capital_usd,
        "funding_total": db.funding_total(),
        "updated": updated,
        "pending": len(db.pending_proposals(now_ms)),
        "positions": [{
            "id": p.id, "base": p.base, "kind": p.kind, "status": p.status,
            "exchanges": sorted({l.exchange for l in p.legs}),
            "funding": sum(l.funding_received for l in p.legs),
            "realized": p.realized_pnl,
            "buffer": buffers.get(str(p.id)),
        } for p in positions],
    }


def format_status(st: dict, cli_hint: bool = True) -> str:
    """cli_hint — подсказка про команду python -m engine proposals (в Telegram не нужна)."""
    mode = ("Автомат (бот открывает связки сам)" if st["mode"] == "auto"
            else "Полуавтомат (каждую связку подтверждаете вы)")
    lines = [
        f"Режим: {mode}",
        "Торговля: " + ("виртуальная (бумажная)" if st["virtual"] else "реальная"),
        "Пауза: " + ("да, новые связки не открываются" if st["paused"] else "нет"),
        "Бот: " + (f"работает (последний сигнал {_utc(st['heartbeat'])})" if st["alive"]
                   else "не запущен" if st["heartbeat"] is None
                   else f"не отвечает (последний сигнал {_utc(st['heartbeat'])})"),
        f"Капитал: {st['equity']:.2f} USD (стартовый {st['capital']:.2f} USD)"
        + (f", данные на {_utc(st['updated'])}" if st["updated"] else ", по ценам входа"),
        f"P&L за день: {st['day_pnl']:+.2f} USD",
        f"P&L всего: {st['pnl_total']:+.2f} USD",
        f"Получено funding всего: {st['funding_total']:+.2f} USD",
    ]
    if st["positions"]:
        lines.append("Связки:")
        for p in st["positions"]:
            buf = "—" if p["buffer"] is None else f"{p['buffer']:.0%}"
            lines.append(
                f"  #{p['id']} {p['base']} {p['kind']} ({', '.join(p['exchanges'])}) — "
                f"{_POS_STATUS_RU.get(p['status'], p['status'])}; funding {p['funding']:+.2f} USD, "
                f"реализовано {p['realized']:+.2f} USD, запас до ликвидации {buf}")
    else:
        lines.append("Открытых связок нет")
    lines.append(f"Предложений ждут подтверждения: {st['pending']}"
                 + (" (python -m engine proposals)" if st["pending"] and cli_hint else ""))
    return "\n".join(lines)


def format_positions(db: Db, settings: Settings, now_ms: int) -> str:
    """Список позиций opening/open/closing; запас до ликвидации — по последним ценам из stats.marks."""
    positions = db.positions(_HELD)
    if not positions:
        return "Открытых связок нет"
    marks = (db.get_kv("stats") or {}).get("marks") or {}
    lines = ["Связки:"]
    for p in positions:
        funding = sum(l.funding_received for l in p.legs)
        lines.append(
            f"#{p.id} {p.base} {p.kind} ({', '.join(sorted({l.exchange for l in p.legs}))}) — "
            f"{_POS_STATUS_RU.get(p.status, p.status)}\n"
            f"  funding {funding:+.2f} USD, реализовано {p.realized_pnl:+.2f} USD")
        for l in p.legs:
            if not (l.open and l.kind == "perp" and l.amount > 0 and l.margin > 0):
                continue
            mark = marks.get(f"{l.exchange}|{l.symbol}")
            buf = "—" if mark is None else f"{leg_buffer(l, mark):.0%}"
            lines.append(f"  запас до ликвидации ({l.exchange}, {l.side}): {buf}")
    return "\n".join(lines)


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Делит текст на части ≤ limit: по переносам строк, слишком длинную строку — по символам.

    Пустых частей (и частей только из пробелов) не бывает: Telegram такие не принимает,
    поэтому пустой текст даёт []. Для обычного текста "\n".join(части) == text.
    """
    parts: list[str] = []
    cur: str | None = None
    for line in text.split("\n"):
        chunks = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for i, chunk in enumerate(chunks):
            if i == 0 and cur is not None and len(cur) + 1 + len(chunk) <= limit:
                cur += "\n" + chunk
                continue
            if cur is not None:
                parts.append(cur)
            cur = chunk
    if cur is not None:
        parts.append(cur)
    return [p for p in parts if p.strip()]
