"""Командная строка движка: python -m engine <команда>."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import signal
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from engine.config import ConfigError, Settings, load_settings
from engine.db import Db
from engine.report import _ALIVE_MS, build_status, format_status


@contextmanager
def _open_db(settings: Settings):
    db = Db(settings.db_path)
    try:
        db.init()
        yield db
    finally:
        db.conn.close()


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


# --- status ---
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


def _cmd_restore(settings: Settings, args) -> int:
    from engine import backup

    heartbeat = None
    if Path(settings.db_path).exists():  # не создаём пустую базу, если файла нет
        db = Db(settings.db_path)
        try:
            heartbeat = db.get_kv("heartbeat")
        except sqlite3.DatabaseError:
            pass  # повреждённая база: проверять нечего, её как раз и восстанавливают
        finally:
            db.conn.close()
    if not args.force and heartbeat is not None and _now_ms() - heartbeat <= _ALIVE_MS:
        print("Бот сейчас запущен. Остановите его (Ctrl+C в окне с python -m engine run), "
              "подождите 3 минуты и повторите: восстановление при работающем боте испортит базу.")
        return 1
    try:
        key = backup.load_key()
        if key is None:
            print("Не задан ключ: export SHIO_BACKUP_KEY=<ключ в base64, тот же, что при создании копий>")
            return 2
        try:
            blob = Path(args.file).read_bytes()
        except OSError as exc:
            print(f"Не удалось прочитать файл {args.file}: {exc}")
            return 1
        old = backup.restore(blob, key, settings.db_path)
    except backup.BackupError as exc:
        print(f"Восстановление не выполнено: {exc}")
        return 1
    print(f"База восстановлена из {args.file}.")
    if old:
        print(f"Прежняя база сохранена: {old}")
    return 0


# --- health (Docker HEALTHCHECK) ---
def _cmd_health(settings: Settings, args) -> int:
    """0 — движок жив (сигнал «жив» не старше 3 минут), 1 — нет; одна строка с причиной."""
    path = settings.db_path
    if not Path(path).exists():  # не создаём пустую базу
        print(f"База не найдена: {path}")
        return 1
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM kv WHERE key='heartbeat'").fetchone()
        finally:
            conn.close()
        heartbeat = None if row is None else int(json.loads(row[0]))
    except (sqlite3.Error, ValueError, TypeError) as exc:
        print(f"База не читается ({path}): {exc}")
        return 1
    if heartbeat is None:
        print("Движок ещё не подал сигнал «жив»")
        return 1
    age_ms = _now_ms() - heartbeat
    if age_ms > _ALIVE_MS:
        print(f"Движок молчит {age_ms // 1000} с")
        return 1
    return 0


# --- run ---
def _setup_logging(settings: Settings) -> None:
    log_path = Path(settings.db_path).parent / "shio.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(getattr(h, "_shio", False) for h in root.handlers):
        return  # уже настроено
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.handlers.RotatingFileHandler(
                        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")):
        handler.setFormatter(fmt)
        handler._shio = True  # type: ignore[attr-defined]
        root.addHandler(handler)


TELEGRAM_BACKOFF_S = 10.0


async def _telegram_supervisor(run, stop: asyncio.Event, backoff_s: float | None = None) -> None:
    """Перезапускает задачу Telegram после сбоя (пауза backoff_s); движок это не затрагивает."""
    log = logging.getLogger("engine")
    pause = TELEGRAM_BACKOFF_S if backoff_s is None else backoff_s
    while not stop.is_set():
        try:
            await run(stop)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — Telegram не должен останавливать торговый цикл
            log.exception("Telegram: задача упала, перезапуск через %g с. Движок работает дальше.",
                          pause)
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=pause)
        except asyncio.TimeoutError:
            pass


def _build_announcement_sources(settings: Settings, http) -> list:
    from engine.launchpool import BybitAnnouncements, OkxAnnouncements

    known = {"bybit": BybitAnnouncements, "okx": OkxAnnouncements}
    sources = []
    for name in settings.launchpool.exchanges:
        if name in known:
            sources.append(known[name](http))
        else:
            logging.getLogger("engine").warning(
                "Launchpool: неизвестная биржа %r в launchpool.exchanges, пропускаю", name)
    return sources


def _log_pinger(pinger) -> None:
    log = logging.getLogger("engine")
    if pinger.url:
        log.info("Сторож включён: %s", pinger.host)
    else:
        log.info("Сторож не настроен: SHIO_HEALTHCHECK_URL пуст")


def _install_signal_handlers(runner) -> None:
    """SIGTERM/SIGINT: дозавершить текущий шаг и выйти; повторный сигнал только пишет в журнал."""
    log = logging.getLogger("engine")
    loop = asyncio.get_running_loop()
    received = []

    def on_signal(signame: str) -> None:
        if received:
            log.warning("Повторный сигнал: дождитесь завершения")
            return
        received.append(signame)
        log.info("Получен сигнал %s: останавливаюсь после текущего шага", signame)
        runner.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, on_signal, sig.name)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # платформа без add_signal_handler (Windows)


async def _run(settings: Settings) -> None:
    from engine.broker import PaperBroker
    from engine.collector import Collector
    from engine.launchpool import AiohttpJson
    from engine.runner import Runner
    from engine.watchdog import HealthPinger
    from engine.telegram import bot as tgbot

    with _open_db(settings) as db:
        collector = Collector(settings)
        stop = asyncio.Event()
        tg_task = None
        notifier = None
        http = AiohttpJson()
        pinger = HealthPinger.from_env()
        _log_pinger(pinger)
        try:
            broker = PaperBroker(collector, settings, db)
            notifier, token = tgbot.build_notifier(settings, db)
            sources = _build_announcement_sources(settings, http)
            runner = Runner(settings, collector, broker, db, notifier,
                            announcement_sources=sources, pinger=pinger)
            _install_signal_handlers(runner)
            if token:
                async def run_tg(stop_event: asyncio.Event) -> None:
                    await tgbot.run_telegram(token, settings, db, notifier, stop_event)

                tg_task = asyncio.ensure_future(_telegram_supervisor(run_tg, stop))
            await runner.run_forever()
        finally:
            stop.set()
            if tg_task is not None:
                try:
                    await asyncio.wait_for(tg_task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    tg_task.cancel()
                    await asyncio.gather(tg_task, return_exceptions=True)
                except Exception:  # noqa: BLE001
                    logging.getLogger("engine").exception("Telegram: ошибка при остановке")
            aclose = getattr(notifier, "aclose", None) if notifier is not None else None
            if aclose is not None:  # дослать очередь (до 5 с) и закрыть сессию бота
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    logging.getLogger("engine").exception("Telegram: ошибка при остановке")
            await pinger.aclose()
            await http.close()
            await collector.close()


def _cmd_telegram_id(settings: Settings, args) -> int:
    import os

    token = os.environ.get("SHIO_TELEGRAM_TOKEN", "").strip()
    if not token:
        print("Не задана переменная окружения SHIO_TELEGRAM_TOKEN "
              "(токен бота от @BotFather): export SHIO_TELEGRAM_TOKEN=...")
        return 2
    from engine.telegram import bot as tgbot

    print("Напишите любое сообщение вашему боту в Telegram (в личном чате). Жду до 2 минут...")
    try:
        sender = asyncio.run(tgbot.discover_chat_id(token, timeout_s=120))
    except KeyboardInterrupt:
        print("Остановлено.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось связаться с Telegram: {exc}")
        return 1
    if sender is None:
        print("За 2 минуты никто не написал боту. Попробуйте ещё раз.")
        return 1
    who = ", ".join(x for x in (sender.name, f"@{sender.username}" if sender.username else None)
                    if x) or "имя не указано"
    if sender.chat_type not in (None, "private") or sender.chat_id != sender.user_id:
        print(f"Сообщение пришло не из личного чата (chat_id {sender.chat_id}, от: {who}). "
              "Бот отвечает только владельцу в личном чате: откройте чат с ботом, "
              "напишите ему и повторите команду.")
        return 1
    print(f"Ваш chat_id: {sender.chat_id} (от: {who}). Проверьте, что это вы, и впишите "
          "число в telegram.chat_id в config.local.yaml (образец — config.local.yaml.example).")
    return 0


async def _doctor(settings: Settings) -> int:
    from engine import doctor
    from engine.launchpool import AiohttpJson
    from engine.watchdog import HealthPinger

    http = AiohttpJson()
    pinger = HealthPinger.from_env()
    try:
        checks = await doctor.run_checks(
            settings, exchange_factory=doctor.CcxtProbe, telegram_getme=doctor.telegram_getme,
            announcement_sources=_build_announcement_sources(settings, http), pinger=pinger,
            db_path=settings.db_path)
    finally:
        await pinger.aclose()
        await http.close()
    print(doctor.format_checks(checks))
    return 0 if all(c.ok for c in checks) else 1


def _cmd_doctor(settings: Settings, args) -> int:
    print("Проверяю сервер (до минуты)...")
    try:
        return asyncio.run(_doctor(settings))
    except KeyboardInterrupt:
        print("Остановлено.")
        return 1


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
    sub.add_parser("telegram-id", help="узнать свой chat_id для Telegram (ждёт сообщение боту 2 минуты)"
                   ).set_defaults(fn=_cmd_telegram_id)
    sp = sub.add_parser("restore", help="восстановить базу из зашифрованной копии (бот должен быть остановлен)")
    sp.add_argument("file", help="файл копии shio-ГГГГ-ММ-ДД.db.enc")
    sp.add_argument("--force", action="store_true",
                    help="не проверять сигнал «жив» (только если бот точно остановлен, "
                         "так делает ./deploy.sh restore)")
    sp.set_defaults(fn=_cmd_restore)
    sub.add_parser("resume", help="снять паузу").set_defaults(fn=_cmd_resume)
    sub.add_parser("health", help="жив ли движок: 0 — да, 1 — нет (для Docker HEALTHCHECK)"
                   ).set_defaults(fn=_cmd_health)
    sub.add_parser("doctor", help="самопроверка сервера: настройки, база, биржи, Telegram, сторож"
                   ).set_defaults(fn=_cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        if args.cmd == "doctor":
            from engine.doctor import Check, format_checks

            print(format_checks([Check("Настройки", False, str(e))]))
            return 1
        if args.cmd == "health":  # Docker понимает только 0 и 1
            print(f"Ошибка в настройках: {' '.join(str(e).split())}")
            return 1
        print(f"Ошибка в настройках: {e}")
        return 2
    return args.fn(settings, args)


if __name__ == "__main__":
    sys.exit(main())
