"""Самопроверка сервера: python -m engine doctor.

Ядро (run_checks) получает все внешние зависимости параметрами — тесты подставляют
подделки, CLI (engine/__main__.py) — настоящие реализации из этого модуля.
Каждая проверка изолирована: таймаут CHECK_TIMEOUT_S, исключение → ❌ с коротким
текстом (имя класса ошибки, без текста исключения: в нём может оказаться секрет).
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

CHECK_TIMEOUT_S = 15.0
CLOCK_SKEW_WARN_S = 2.0
TOKEN_ENV = "SHIO_TELEGRAM_TOKEN"
KEY_ENV = "SHIO_BACKUP_KEY"
HEALTHCHECK_ENV = "SHIO_HEALTHCHECK_URL"
_GEO_RE = re.compile(r"\b(403|451)\b")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False


def _ok(name: str, detail: str) -> Check:
    return Check(name, True, detail)


def _warn(name: str, detail: str) -> Check:
    return Check(name, True, detail, warn=True)


def _fail(name: str, detail: str) -> Check:
    return Check(name, False, detail)


def _default_error(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"таймаут: нет ответа за {CHECK_TIMEOUT_S:g} с"
    return f"ошибка: {type(exc).__name__}"


async def _guarded(name: str, fn: Callable[[], Awaitable[list[Check]]],
                   on_error: Callable[[BaseException], str] = _default_error) -> list[Check]:
    """Выполнить проверку с таймаутом; любая ошибка — строка ❌, не исключение."""
    try:
        return await asyncio.wait_for(fn(), CHECK_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — doctor не должен падать
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return [_fail(name, on_error(exc))]


# --- 1. настройки ---
def _check_config(settings) -> list[Check]:
    if settings.virtual:
        mode = "автоматический" if settings.mode == "auto" else "полуавтоматический"
        return [_ok("Настройки", f"загружены; торговля виртуальная (бумажная), режим {mode}")]
    return [_fail("Настройки", "engine.virtual: false — реальная торговля появится на этапе 5, "
                               "бот не запустится; укажите engine.virtual: true")]


# --- 2. переменные окружения (значения никогда не выводим) ---
def _check_env(settings) -> list[Check]:
    from engine import backup

    out = []
    if os.environ.get(TOKEN_ENV, "").strip():
        out.append(_ok(TOKEN_ENV, "задан"))
    elif settings.telegram.enabled:
        out.append(_fail(TOKEN_ENV, "не задан, а telegram.enabled: true — впишите токен "
                                    "бота от @BotFather в .env"))
    else:
        out.append(_warn(TOKEN_ENV, "не задан (Telegram выключен в настройках)"))
    try:
        key = backup.load_key()
    except backup.BackupError:
        out.append(_fail(KEY_ENV, "задан, но неверный: нужен base64 от 32 байт "
                                  "(сгенерировать: openssl rand -base64 32)"))
    else:
        if key is None:
            out.append(_warn(KEY_ENV, "не задан — резервные копии не создаются"))
        else:
            out.append(_ok(KEY_ENV, "задан, формат верный"))
    if os.environ.get(HEALTHCHECK_ENV, "").strip():
        out.append(_ok(HEALTHCHECK_ENV, "задан"))
    else:
        out.append(_warn(HEALTHCHECK_ENV, "не задан — внешний сторож не будет знать, "
                                          "что бот остановился"))
    return out


# --- 3. каталог данных и база ---
def _check_db_sync(db_path: str) -> list[Check]:
    from engine.db import Db

    data_dir = Path(db_path).parent
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".doctor-", delete=True) as f:
            f.write(b"ok")
            f.flush()
    except OSError as exc:
        return [_fail("База данных", f"каталог {data_dir} недоступен для записи: "
                                     f"{type(exc).__name__}")]
    try:
        db = Db(db_path)
    except (OSError, sqlite3.Error) as exc:
        return [_fail("База данных", f"не открывается {db_path}: {type(exc).__name__}")]
    try:
        db.init()
        mode = str(db.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    except sqlite3.Error as exc:
        return [_fail("База данных", f"{db_path}: {type(exc).__name__} "
                                     "(файл повреждён или от другой версии?)")]
    finally:
        db.conn.close()
    if mode != "wal":
        return [_fail("База данных", f"{db_path}: режим журнала {mode}, ожидался WAL")]
    return [_ok("База данных", f"{db_path} открывается, каталог доступен для записи, режим WAL")]


async def _check_db(db_path: str) -> list[Check]:
    return await asyncio.to_thread(_check_db_sync, db_path)


# --- 4. биржи ---
def _exchange_error(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        cls = f"таймаут {CHECK_TIMEOUT_S:g} с"
    else:
        cls = type(exc).__name__
    detail = f"биржа недоступна с этого сервера: {cls}"
    if _GEO_RE.search(str(exc)):
        detail += (" — похоже, биржа заблокирована для страны сервера (HTTP 403/451); "
                   "нужен VPS в другой стране")
    return detail


async def _check_exchange(name: str, probe) -> list[Check]:
    start = time.monotonic()
    count = await probe.check()
    ms = int((time.monotonic() - start) * 1000)
    return [_ok(f"Биржа {name}", f"отвечает: {count} фьючерсов, {ms} мс")]


# --- 5. Telegram ---
async def _check_telegram(settings, getme) -> list[Check]:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        if settings.telegram.enabled:
            return [_fail("Telegram", f"не проверен: не задан {TOKEN_ENV}")]
        return [_warn("Telegram", "выключен (telegram.enabled: false)")]
    return [_ok("Telegram", f"бот @{await getme(token)} отвечает")]


def _check_chat_id(settings) -> list[Check]:
    if settings.telegram.chat_id is not None:
        return [_ok("Telegram chat_id", "задан в config.yaml")]
    if settings.telegram.enabled:
        return [_fail("Telegram chat_id", "не задан в config.yaml — узнайте его командой "
                                          "python -m engine telegram-id")]
    return [_warn("Telegram chat_id", "не задан (Telegram выключен)")]


# --- 6. Launchpool ---
async def _check_source(name: str, src) -> list[Check]:
    items = await src.fetch()
    titles = "; ".join(f"«{a.title}»" for a in items[:2])
    detail = f"{len(items)} объявлений" + (f": {titles}" if titles else "")
    if not items:
        return [_warn(name, "0 объявлений — проверьте, что раздел объявлений верный")]
    return [_ok(name, detail)]


# --- 7. сторож ---
async def _check_pinger(pinger) -> list[Check]:
    if not pinger.url:
        return [_warn("Сторож", f"не настроен ({HEALTHCHECK_ENV} пуст), пинг не отправлен")]
    if await pinger.ping():
        return [_ok("Сторож", f"пинг на {pinger.host} прошёл")]
    return [_warn("Сторож", f"пинг на {pinger.host} не прошёл — проверьте адрес и сеть")]


# --- 8. часы ---
async def _check_clock(candidates: list[tuple[str, object]]) -> list[Check]:
    for name, probe in candidates:
        try:
            before = time.time()
            ex_ms = await asyncio.wait_for(probe.fetch_time(), CHECK_TIMEOUT_S)
            local_ms = (before + time.time()) / 2 * 1000
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — пробуем следующую биржу
            continue
        skew = abs(float(ex_ms) - local_ms) / 1000
        if skew > CLOCK_SKEW_WARN_S:
            return [_warn("Часы сервера", f"расходятся с {name} на {skew:.1f} с — включите "
                                          "синхронизацию времени (timedatectl set-ntp true)")]
        return [_ok("Часы сервера", f"расхождение с {name} {skew:.1f} с")]
    return [_warn("Часы сервера", "не проверены: ни одна торговая биржа не ответила")]


async def run_checks(settings, *, exchange_factory, telegram_getme, announcement_sources,
                     pinger, db_path) -> list[Check]:
    checks: list[Check] = []
    checks += _check_config(settings)
    checks += await _guarded("Переменные окружения", lambda: _async(_check_env, settings))
    checks += await _guarded("База данных", lambda: _check_db(db_path))

    watched = [n for n, cfg in settings.exchanges.items() if cfg.watch]
    probes: dict[str, object] = {}

    async def one(name: str) -> list[Check]:
        async def body() -> list[Check]:
            probe = exchange_factory(name)
            probes[name] = probe
            return await _check_exchange(name, probe)
        return await _guarded(f"Биржа {name}", body, _exchange_error)

    try:
        results = await asyncio.gather(*(one(n) for n in watched))
        reachable = set()
        for name, res in zip(watched, results):
            checks += res
            if all(c.ok for c in res):
                reachable.add(name)

        checks += await _guarded("Telegram", lambda: _check_telegram(settings, telegram_getme))
        checks += _check_chat_id(settings)

        if settings.launchpool.enabled:
            for src in announcement_sources:
                name = f"Launchpool {getattr(src, 'exchange', '?')}"
                checks += await _guarded(name, lambda src=src, name=name: _check_source(name, src))
        else:
            checks.append(_warn("Launchpool", "выключен (launchpool.enabled: false)"))

        checks += await _guarded("Сторож", lambda: _check_pinger(pinger))

        trading = [(n, probes[n]) for n, cfg in settings.exchanges.items()
                   if cfg.trade and n in reachable]
        checks += await _guarded("Часы сервера", lambda: _check_clock(trading))
    finally:
        for probe in probes.values():
            try:
                await asyncio.wait_for(probe.close(), 5)
            except Exception:  # noqa: BLE001
                pass
    return checks


async def _async(fn, *args):
    return fn(*args)


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "❌" if not c.ok else ("⚠️" if c.warn else "✅")
        lines.append(f"{mark} {c.name} — {c.detail}")
    fails = sum(1 for c in checks if not c.ok)
    warns = sum(1 for c in checks if c.ok and c.warn)
    lines.append("")
    if fails:
        lines.append(f"Есть проблемы: {fails}")
    elif warns:
        lines.append(f"Всё в порядке (предупреждений: {warns})")
    else:
        lines.append("Всё в порядке")
    return "\n".join(lines)


# --- настоящие реализации для CLI ---
class CcxtProbe:
    """Публичные запросы к бирже через ccxt (без ключей)."""

    def __init__(self, name: str):
        from scanner.exchanges import ExchangeClient

        self.client = ExchangeClient(name)
        self.ex = self.client.ex

    async def check(self) -> int:
        markets = await self.ex.load_markets()
        perps = [m for m in markets.values() if m.get("swap") and m.get("linear")]
        btc = next((m["symbol"] for m in perps if m.get("base") == "BTC"), None)
        if btc is None:
            raise LookupError("нет фьючерса BTC")
        if self.ex.has.get("fetchFundingRates") is True:
            await self.ex.fetch_funding_rates([btc])
        else:
            await self.ex.fetch_ticker(btc)
        return len(perps)

    async def fetch_time(self) -> int:
        return int(await self.ex.fetch_time())

    async def close(self) -> None:
        await self.client.close()


async def telegram_getme(token: str) -> str:
    from engine.telegram.bot import make_bot

    bot = make_bot(token)
    try:
        me = await bot.get_me()
        return me.username or str(me.id)
    finally:
        await bot.session.close()
