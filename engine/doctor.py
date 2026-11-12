"""Самопроверка сервера: python -m engine doctor.

Ядро (run_checks) получает все внешние зависимости параметрами — тесты подставляют
подделки, CLI (engine/__main__.py) — настоящие реализации из этого модуля.
Каждая проверка изолирована: таймаут, исключение → ❌ с короткой русской подсказкой
и именем класса ошибки. Текст исключения не выводится никогда: в нём может оказаться
секрет (токен в URL Telegram, адрес сторожа).
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

CHECK_TIMEOUT_S = 15.0       # предел на одну проверку (и на первый запрос к бирже)
EXCHANGE_TIMEOUT_S = 40.0    # общий бюджет на одну биржу (время + рынки + BTC)
CLOCK_SKEW_WARN_S = 2.0
TOKEN_ENV = "SHIO_TELEGRAM_TOKEN"
KEY_ENV = "SHIO_BACKUP_KEY"
HEALTHCHECK_ENV = "SHIO_HEALTHCHECK_URL"
HEALTHCHECK_SUFFIX = "/log"  # healthchecks.io: событие в журнал, статус проверки не меняется
_GEO_RE = re.compile(r"403|451|restricted location|not available in your (country|region)"
                     r"|compliance|Forbidden|CloudFront|51155", re.IGNORECASE)


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


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or \
        type(exc).__name__ == "RequestTimeout"


def _class_names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def _error_text(exc: BaseException, timeout_s: float | None = None) -> str:
    """Русская подсказка по классу ошибки (без текста исключения)."""
    cls = type(exc).__name__
    if _is_timeout(exc):
        return f"таймаут: нет ответа за {timeout_s or CHECK_TIMEOUT_S:g} с"
    names = _class_names(exc)
    if names & {"TelegramUnauthorizedError", "TokenValidationError"}:
        return f"токен бота неверный, возьмите новый у @BotFather ({cls})"
    if "ClientResponseError" in names:
        return f"сервер ответил ошибкой, возможна блокировка ({cls})"
    if names & {"ClientConnectorDNSError", "ClientConnectorError", "ConnectionError", "OSError"}:
        return f"нет доступа в интернет или DNS ({cls})"
    return f"ошибка: {cls}"


async def _guarded(name: str, fn: Callable[[], Awaitable[list[Check]]],
                   on_error: Callable[[BaseException], str] = _error_text) -> list[Check]:
    """Выполнить проверку с таймаутом; любая ошибка — строка ❌, не исключение."""
    try:
        return await asyncio.wait_for(fn(), CHECK_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — doctor не должен падать
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


async def _env_checks(settings) -> list[Check]:
    return _check_env(settings)


# --- 3. каталог данных и база ---
def _check_db_sync(db_path: str) -> list[Check]:
    from engine.db import Db

    data_dir = Path(db_path).parent
    created = not data_dir.exists()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".doctor-", delete=True) as f:
            f.write(b"ok")
            f.flush()
    except OSError as exc:
        return [_fail("База данных", f"каталог {data_dir} недоступен для записи: "
                                     f"{type(exc).__name__}")]
    note = f"каталог {data_dir} не существовал — каталог создан; " if created else ""
    try:
        db = Db(db_path)
    except (OSError, sqlite3.Error) as exc:
        return [_fail("База данных", f"{note}не открывается {db_path}: {type(exc).__name__}")]
    try:
        db.init()
        mode = str(db.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    except sqlite3.Error as exc:
        return [_fail("База данных", f"{note}{db_path}: {type(exc).__name__} "
                                     "(файл повреждён или от другой версии?)")]
    finally:
        db.conn.close()
    if mode != "wal":
        return [_fail("База данных", f"{note}{db_path}: режим журнала {mode}, ожидался WAL")]
    return [_ok("База данных", f"{note}{db_path} открывается, каталог доступен для записи, "
                               "режим WAL")]


async def _check_db(db_path: str) -> list[Check]:
    return await asyncio.to_thread(_check_db_sync, db_path)


# --- 4. биржи ---
def _exchange_error(exc: BaseException, timeout_s: float) -> str:
    cls = f"таймаут {timeout_s:g} с" if _is_timeout(exc) else type(exc).__name__
    detail = f"биржа недоступна с этого сервера: {cls}"
    # текст исключения только сопоставляем с шаблонами, но не выводим
    if _GEO_RE.search(str(exc)):
        detail += (" — похоже, биржа заблокирована для страны сервера (HTTP 403/451); "
                   "нужен VPS в другой стране")
    elif _is_timeout(exc) or "ExchangeNotAvailable" in _class_names(exc):
        detail += " — возможно, блокировка по стране сервера"
    return detail


@dataclass
class _ExchangeResult:
    check: Check
    time_ms: float | None = None   # время биржи
    local_ms: float | None = None  # наше время в момент ответа


async def _probe_exchange(name: str, exchange_factory) -> tuple[_ExchangeResult, object | None]:
    label = f"Биржа {name}"
    budget = EXCHANGE_TIMEOUT_S
    start = time.monotonic()
    try:
        probe = exchange_factory(name)
    except Exception as exc:  # noqa: BLE001
        return _ExchangeResult(_fail(label, f"не создать клиент: {type(exc).__name__}")), None

    # шаг 1: время биржи — доказывает доступность и нужно для проверки часов
    first = min(CHECK_TIMEOUT_S, budget)
    ex_ms = local_ms = None
    try:
        before = time.time()
        ex_ms = float(await asyncio.wait_for(probe.fetch_time(), first))
        local_ms = (before + time.time()) / 2 * 1000
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "NotSupported":
            return _ExchangeResult(_fail(label, _exchange_error(exc, first))), probe

    # шаг 2: рынки и BTC-фьючерс в оставшемся бюджете
    left = max(0.0, budget - (time.monotonic() - start))
    try:
        count = await asyncio.wait_for(probe.check(), left)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_timeout(exc) and ex_ms is not None:
            chk = _warn(label, f"доступна, но отвечает медленно: рынки не загрузились "
                               f"за {budget:g} с")
            return _ExchangeResult(chk, ex_ms, local_ms), probe
        return _ExchangeResult(_fail(label, _exchange_error(exc, left)), ex_ms, local_ms), probe
    ms = int((time.monotonic() - start) * 1000)
    chk = _ok(label, f"отвечает: {count} фьючерсов, {ms} мс")
    return _ExchangeResult(chk, ex_ms, local_ms), probe


# --- 5. Telegram ---
async def _check_telegram(settings, getme) -> list[Check]:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        if settings.telegram.enabled:
            return [_warn("Telegram", f"пропущено: нет токена ({TOKEN_ENV})")]
        return [_warn("Telegram", "выключен (telegram.enabled: false)")]
    return [_ok("Telegram", f"бот @{await getme(token)} отвечает")]


EXAMPLE_CHAT_ID = 123456789  # заглушка из config.local.yaml.example


def _check_chat_id(settings) -> list[Check]:
    if settings.telegram.chat_id == EXAMPLE_CHAT_ID:
        return [_fail("Telegram chat_id", "chat_id — это пример из образца, впишите свой "
                                          "(python -m engine telegram-id)")]
    if settings.telegram.chat_id is not None:
        return [_ok("Telegram chat_id", "задан")]
    if settings.telegram.enabled:
        return [_fail("Telegram chat_id", "не задан — узнайте его командой "
                                          "python -m engine telegram-id и впишите в config.local.yaml")]
    return [_warn("Telegram chat_id", "не задан (Telegram выключен)")]


# --- 6. Launchpool ---
async def _check_source(name: str, src) -> list[Check]:
    items = await src.fetch()
    if not items:
        return [_warn(name, "0 объявлений — проверьте, что раздел объявлений верный")]
    titles = "; ".join(f"«{a.title}»" for a in items[:2])
    return [_ok(name, f"{len(items)} объявлений: {titles}")]


# --- 7. сторож ---
async def _check_pinger(pinger) -> list[Check]:
    if not pinger.url:
        return [_warn("Сторож", f"не настроен ({HEALTHCHECK_ENV} пуст), пинг не отправлен")]
    if await pinger.ping(suffix=HEALTHCHECK_SUFFIX):
        return [_ok("Сторож", f"адрес сторожа доступен ({pinger.host})")]
    return [_warn("Сторож", f"адрес сторожа {pinger.host} не ответил — проверьте адрес и сеть")]


# --- 8. часы ---
def _check_clock(settings, results: dict[str, _ExchangeResult]) -> list[Check]:
    for name, cfg in settings.exchanges.items():
        r = results.get(name)
        if not cfg.trade or r is None or r.time_ms is None:
            continue
        skew = abs(r.time_ms - r.local_ms) / 1000
        if skew > CLOCK_SKEW_WARN_S:
            return [_warn("Часы сервера", f"расходятся с {name} на {skew:.1f} с — включите "
                                          "синхронизацию времени (timedatectl set-ntp true)")]
        return [_ok("Часы сервера", f"расхождение с {name} {skew:.1f} с")]
    return [_warn("Часы сервера", "не проверены: ни одна торговая биржа не сообщила время")]


async def run_checks(settings, *, exchange_factory, telegram_getme, announcement_sources,
                     pinger, db_path) -> list[Check]:
    checks: list[Check] = []
    checks += _check_config(settings)
    checks += await _guarded("Переменные окружения", lambda: _env_checks(settings))
    checks += await _guarded("База данных", lambda: _check_db(db_path))

    watched = [n for n, cfg in settings.exchanges.items() if cfg.watch]
    probes: list[object] = []
    try:
        pairs = await asyncio.gather(*(_probe_exchange(n, exchange_factory) for n in watched))
        results: dict[str, _ExchangeResult] = {}
        for name, (res, probe) in zip(watched, pairs):
            results[name] = res
            checks.append(res.check)
            if probe is not None:
                probes.append(probe)

        checks += await _guarded("Telegram", lambda: _check_telegram(settings, telegram_getme))
        checks += _check_chat_id(settings)

        if not settings.launchpool.enabled:
            checks.append(_warn("Launchpool", "выключен (launchpool.enabled: false)"))
        elif not announcement_sources:
            checks.append(_warn("Launchpool", "нет источников (launchpool.exchanges пуст "
                                              "или биржи неизвестны)"))
        else:
            for src in announcement_sources:
                name = f"Launchpool {getattr(src, 'exchange', '?')}"
                checks += await _guarded(name, lambda src=src, name=name: _check_source(name, src))

        checks += await _guarded("Сторож", lambda: _check_pinger(pinger))
        checks += _check_clock(settings, results)
    finally:
        for probe in probes:
            try:
                await asyncio.wait_for(probe.close(), 5)
            except Exception:  # noqa: BLE001
                pass
    return checks


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

        self.name = name
        self.client = ExchangeClient(name)
        self.ex = self.client.ex

    async def fetch_time(self) -> int:
        import ccxt

        if not self.ex.has.get("fetchTime"):
            raise ccxt.NotSupported(f"{self.name}: fetchTime")
        return int(await self.ex.fetch_time())

    async def check(self) -> int:
        import ccxt

        from scanner.exchanges import _linear_perp, _quotes

        markets = await self.ex.load_markets()
        perps = [m for m in markets.values() if _linear_perp(self.name, m)]
        btc = next((f"BTC/{q}:{q}" for q in _quotes(self.name) if f"BTC/{q}:{q}" in markets),
                   None)
        if btc is None:
            raise LookupError("нет фьючерса BTC")
        try:
            await self.ex.fetch_funding_rates([btc])
        except (ccxt.NotSupported, ccxt.ArgumentsRequired, ccxt.BadSymbol):
            await self.ex.fetch_ticker(btc)
        return len(perps)

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
