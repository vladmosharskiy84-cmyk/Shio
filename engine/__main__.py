"""Командная строка движка: python -m engine <команда>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from engine import ledger
from engine.collector import MarketSnapshot
from engine.config import ConfigError, Settings, load_settings
from engine.db import Db

_HELD = {"opening", "open", "closing"}
_ALIVE_MS = 3 * 60_000  # сигнал «жив» не старше 3 минут — бот работает
_POS_STATUS_RU = {"opening": "открывается", "open": "открыта", "closing": "закрывается"}


@contextmanager
def _open_db(settings: Settings):
    db = Db(settings.db_path)
    try:
        db.init()
        yield db
    finally:
        db.conn.close()


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


# --- status ---
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


def format_status(st: dict) -> str:
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
                 + (" (python -m engine proposals)" if st["pending"] else ""))
    return "\n".join(lines)


def _cmd_status(settings: Settings, args) -> int:
    with _open_db(settings) as db:
        print(format_status(build_status(db, settings, _now_ms())))
    return 0


def _cmd_proposals(settings: Settings, args) -> int:
    now = _now_ms()
    with _open_db(settings) as db:
        props = db.pending_proposals(now)
    if not props:
        print("Предложений, ожидающих подтверждения, нет.")
        return 0
    print("Ждут подтверждения:")
    for p in props:
        legs = ", ".join(f"{l['side']} {l['kind']} на {l['exchange']}" for l in p.legs_plan)
        profile = "агрессивный" if p.profile == "aggressive" else "спокойный"
        left = max(0, (p.expires_at - now) // 60_000)
        print(f"  #{p.id} {p.base} {p.kind} ({legs}), профиль {profile}, капитал {p.capital:.2f} USD, "
              f"{p.expected_apr:.1f}% годовых (~{p.expected_profit:.2f} USD), "
              f"осталось {left} мин")
    print("Подтвердить: python -m engine approve <номер>; отклонить: python -m engine reject <номер>")
    return 0


# --- команды для работающего бота ---
def _send(settings: Settings, type_: str, arg: str | None, what: str) -> int:
    with _open_db(settings) as db:
        cid = db.add_command(type_, arg)
        heartbeat = db.get_kv("heartbeat")
    print(f"Команда #{cid} принята: {what}. Её выполнит работающий бот в течение нескольких секунд.")
    if heartbeat is None or _now_ms() - heartbeat > _ALIVE_MS:
        print("Внимание: бот сейчас не запущен — команда выполнится после запуска "
              "(python -m engine run).")
    return 0


def _cmd_approve(settings, args) -> int:
    return _send(settings, "approve", str(args.id), f"подтвердить предложение #{args.id}")


def _cmd_reject(settings, args) -> int:
    return _send(settings, "reject", str(args.id), f"отклонить предложение #{args.id}")


def _cmd_close(settings, args) -> int:
    return _send(settings, "close", str(args.id), f"закрыть связку #{args.id}")


def _cmd_pause(settings, args) -> int:
    return _send(settings, "pause", None, "пауза (новые связки не открываются)")


def _cmd_resume(settings, args) -> int:
    return _send(settings, "resume", None, "снять паузу")


def _cmd_stop(settings, args) -> int:
    answer = input("Закрыть ВСЕ связки и поставить бота на паузу? Введите «да» для подтверждения: ")
    if answer.strip().lower() != "да":
        print("Отменено.")
        return 0
    return _send(settings, "stop_all", None, "закрыть все связки и поставить на паузу")


# --- run ---
def _setup_logging(settings: Settings) -> None:
    log_path = Path(settings.db_path).parent / "shio.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)


async def _run(settings: Settings) -> None:
    from engine.broker import PaperBroker
    from engine.collector import Collector
    from engine.notifier import LogNotifier
    from engine.runner import Runner

    with _open_db(settings) as db:
        collector = Collector(settings)
        try:
            broker = PaperBroker(collector, settings, db)
            runner = Runner(settings, collector, broker, db, LogNotifier(db))
            await runner.run_forever()
        finally:
            await collector.close()


def _cmd_run(settings: Settings, args) -> int:
    if not settings.virtual:
        print("Реальная торговля появится на этапе 5. Сейчас движок работает только "
              "в виртуальном режиме: укажите engine.virtual: true в config.yaml.")
        return 2
    _setup_logging(settings)
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        logging.getLogger("engine").info("Остановлено пользователем (Ctrl+C).")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m engine",
                                description="Shio: движок funding-арбитража")
    p.add_argument("--config", default="config.yaml", help="файл настроек (по умолчанию config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="команда")
    sub.add_parser("run", help="запустить бота (виртуальная торговля)").set_defaults(fn=_cmd_run)
    sub.add_parser("status", help="капитал, P&L, связки").set_defaults(fn=_cmd_status)
    sub.add_parser("proposals", help="предложения, ждущие подтверждения").set_defaults(
        fn=_cmd_proposals)
    for name, fn, what in (("approve", _cmd_approve, "номер предложения"),
                           ("reject", _cmd_reject, "номер предложения"),
                           ("close", _cmd_close, "номер связки")):
        sp = sub.add_parser(name, help={"approve": "подтвердить предложение",
                                        "reject": "отклонить предложение",
                                        "close": "закрыть связку"}[name])
        sp.add_argument("id", type=int, help=what)
        sp.set_defaults(fn=fn)
    sub.add_parser("stop", help="закрыть все связки и поставить на паузу").set_defaults(fn=_cmd_stop)
    sub.add_parser("pause", help="не открывать новые связки").set_defaults(fn=_cmd_pause)
    sub.add_parser("resume", help="снять паузу").set_defaults(fn=_cmd_resume)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        print(f"Ошибка в настройках: {e}")
        return 2
    return args.fn(settings, args)


if __name__ == "__main__":
    sys.exit(main())
