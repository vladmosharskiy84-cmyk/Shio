import asyncio
import base64
import time

import pytest

from engine import doctor
from engine.config import ExchangeCfg, load_settings
from engine.doctor import Check, format_checks, run_checks
from engine.launchpool import Announcement

TOKEN = "123456:SECRET-TOKEN-VALUE"
KEY = base64.b64encode(b"k" * 32).decode()
HC_URL = "https://hc-ping.com/secret-uuid-777"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SHIO_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("SHIO_BACKUP_KEY", KEY)
    monkeypatch.setenv("SHIO_HEALTHCHECK_URL", HC_URL)


def settings(tmp_path):
    s = load_settings("config.yaml")
    s.exchanges = {
        "bybit": ExchangeCfg("bybit", True, True, 0.001, 0.0005),
        "okx": ExchangeCfg("okx", True, True, 0.001, 0.0005),
        "binance": ExchangeCfg("binance", False, True, 0.001, 0.0005),
    }
    s.db_path = str(tmp_path / "data" / "shio.db")
    s.telegram.enabled = True
    s.telegram.chat_id = 42
    s.launchpool.enabled = True
    return s


class FakeProbe:
    def __init__(self, name, exc=None, hang=False, time_ms=None, count=300, time_exc=None,
                 hang_time=False):
        self.name, self.exc, self.hang, self.count = name, exc, hang, count
        self.time_ms, self.time_exc, self.hang_time = time_ms, time_exc, hang_time
        self.closed = False

    async def check(self):
        if self.hang:
            await asyncio.sleep(60)
        if self.exc:
            raise self.exc
        return self.count

    async def fetch_time(self):
        if self.hang_time:
            await asyncio.sleep(60)
        if self.time_exc:
            raise self.time_exc
        return self.time_ms if self.time_ms is not None else int(time.time() * 1000)

    async def close(self):
        self.closed = True


class Factory:
    def __init__(self, **special):
        self.special = special
        self.probes = {}

    def __call__(self, name):
        p = self.special.get(name) or FakeProbe(name)
        self.probes[name] = p
        return p


class FakeSource:
    def __init__(self, exchange, titles):
        self.exchange = exchange
        self.titles = titles

    async def fetch(self):
        return [Announcement(self.exchange, f"u{i}", t, f"https://x/{i}", 0)
                for i, t in enumerate(self.titles)]


class FakePinger:
    def __init__(self, url=HC_URL, ok=True):
        self.url, self.ok = url, ok
        self.calls = []

    @property
    def host(self):
        return "hc-ping.com"

    async def ping(self, fail=False, suffix=""):
        self.calls.append((fail, suffix))
        return self.ok


async def getme_ok(token):
    assert token == TOKEN
    return "shio_test_bot"


def do(tmp_path, factory=None, sources=None, pinger=None, getme=getme_ok, s=None):
    s = s or settings(tmp_path)
    if sources is None:
        sources = [FakeSource("bybit", ["Bybit Launchpool: AAA", "New listing BBB", "CCC"]),
                   FakeSource("okx", ["OKX Jumpstart: DDD"])]
    return run(run_checks(s, exchange_factory=factory or Factory(), telegram_getme=getme,
                          announcement_sources=sources, pinger=pinger or FakePinger(),
                          db_path=s.db_path))


def failed(checks):
    return [c for c in checks if not c.ok]


def test_all_ok_exit_zero(tmp_path, capsys):
    checks = do(tmp_path)
    assert failed(checks) == []
    assert not any(c.warn for c in checks), [c for c in checks if c.warn]
    text = format_checks(checks)
    assert "❌" not in text
    assert text.strip().splitlines()[-1].startswith("Всё в порядке")
    assert "@shio_test_bot" in text
    assert "виртуальн" in text
    for name in ("bybit", "okx", "binance"):
        assert any(name in c.name for c in checks)


def test_doctor_continues_after_failure(tmp_path):
    import ccxt

    f = Factory(okx=FakeProbe("okx", time_exc=ccxt.ExchangeNotAvailable("okx down")))
    checks = do(tmp_path, factory=f)
    bad = failed(checks)
    assert len(bad) == 1 and "okx" in bad[0].name
    assert "биржа недоступна с этого сервера: ExchangeNotAvailable" in bad[0].detail
    ok_names = [c.name for c in checks if c.ok and not c.warn]
    assert any("bybit" in n for n in ok_names) and any("binance" in n for n in ok_names)
    # проверки после бирж тоже выполнились
    assert any("@shio_test_bot" in c.detail for c in checks)
    assert "Есть проблемы: 1" in format_checks(checks)
    assert all(p.closed for p in f.probes.values())


def test_timeout_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT_S", 0.1)
    f = Factory(binance=FakeProbe("binance", hang_time=True))
    start = time.monotonic()
    checks = do(tmp_path, factory=f)
    assert time.monotonic() - start < 5
    bad = failed(checks)
    assert len(bad) == 1 and "binance" in bad[0].name
    assert "таймаут" in bad[0].detail


def test_slow_after_time_is_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT_S", 0.1)
    monkeypatch.setattr(doctor, "EXCHANGE_TIMEOUT_S", 0.3)
    checks = do(tmp_path, factory=Factory(binance=FakeProbe("binance", hang=True)))
    assert failed(checks) == []
    b = next(c for c in checks if "binance" in c.name)
    assert b.warn and "доступна, но отвечает медленно" in b.detail


def test_markets_error_after_time_is_failure(tmp_path):
    bad = failed(do(tmp_path, factory=Factory(bybit=FakeProbe("bybit", exc=LookupError("x")))))
    assert len(bad) == 1 and "bybit" in bad[0].name


def test_hanging_getme_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT_S", 0.1)

    async def hang(token):
        await asyncio.sleep(60)

    bad = failed(do(tmp_path, getme=hang))
    assert len(bad) == 1 and "Telegram" in bad[0].name


def test_secrets_never_printed(tmp_path):
    async def getme_leaky(token):
        raise RuntimeError(f"Unauthorized for https://api.telegram.org/bot{token}/getMe")

    f = Factory(bybit=FakeProbe("bybit", time_exc=RuntimeError(f"boom {HC_URL} {KEY}")))
    checks = do(tmp_path, factory=f, getme=getme_leaky, pinger=FakePinger(ok=False))
    text = format_checks(checks)
    for secret in (TOKEN, "SECRET-TOKEN-VALUE", KEY, HC_URL, "secret-uuid-777"):
        assert secret not in text
    assert "SHIO_TELEGRAM_TOKEN" in text and "SHIO_BACKUP_KEY" in text


def test_geo_block_hint(tmp_path):
    import ccxt

    exc = ccxt.ExchangeNotAvailable("binance GET https://fapi.binance.com/x 451 "
                                    "Service unavailable from a restricted location")
    checks = do(tmp_path, factory=Factory(binance=FakeProbe("binance", time_exc=exc)))
    bad = failed(checks)
    assert len(bad) == 1
    assert "заблокирована для страны сервера" in bad[0].detail
    assert "restricted location" not in bad[0].detail


@pytest.mark.parametrize("msg", ["okx {\"code\":\"51155\"}", "403 Forbidden",
                                 "Request blocked by CloudFront",
                                 "This service is not available in your region",
                                 "compliance restriction"])
def test_geo_block_hint_patterns(tmp_path, msg):
    import ccxt

    exc = ccxt.ExchangeError(msg)
    bad = failed(do(tmp_path, factory=Factory(okx=FakeProbe("okx", time_exc=exc))))
    assert "заблокирована для страны сервера" in bad[0].detail


def test_soft_geo_hint_for_not_available(tmp_path):
    import ccxt

    exc = ccxt.ExchangeNotAvailable("okx GET https://x 503")
    bad = failed(do(tmp_path, factory=Factory(okx=FakeProbe("okx", time_exc=exc))))
    assert "возможно, блокировка по стране сервера" in bad[0].detail


def test_no_geo_hint_without_403(tmp_path):
    import ccxt

    exc = ccxt.BadResponse("binance GET https://fapi.binance.com/x garbage")
    bad = failed(do(tmp_path, factory=Factory(binance=FakeProbe("binance", time_exc=exc))))
    assert "блокировк" not in bad[0].detail and "заблокирована" not in bad[0].detail


def test_launchpool_titles_shown(tmp_path):
    checks = do(tmp_path)
    lp = [c for c in checks if "Launchpool" in c.name]
    assert len(lp) == 2
    bybit = next(c for c in lp if "bybit" in c.name)
    assert bybit.ok and "3" in bybit.detail
    assert "Bybit Launchpool: AAA" in bybit.detail and "New listing BBB" in bybit.detail
    assert "CCC" not in bybit.detail


def test_launchpool_source_error(tmp_path):
    class Broken:
        exchange = "okx"

        async def fetch(self):
            raise OSError("down")

    bad = failed(do(tmp_path, sources=[Broken()]))
    assert len(bad) == 1 and "okx" in bad[0].name


def test_clock_skew_warn(tmp_path):
    skewed = FakeProbe("bybit", time_ms=int(time.time() * 1000) + 5_000)
    checks = do(tmp_path, factory=Factory(bybit=skewed))
    clock = next(c for c in checks if "Часы" in c.name)
    assert clock.ok and clock.warn
    assert failed(checks) == []


def test_clock_skew_ok(tmp_path):
    clock = next(c for c in do(tmp_path) if "Часы" in c.name)
    assert clock.ok and not clock.warn


def test_clock_uses_next_trading_exchange_when_first_down(tmp_path):
    import ccxt

    f = Factory(bybit=FakeProbe("bybit", time_exc=ccxt.NetworkError("x")),
                okx=FakeProbe("okx", time_ms=int(time.time() * 1000) + 10_000))
    clock = next(c for c in do(tmp_path, factory=f) if "Часы" in c.name)
    assert clock.warn and "okx" in clock.detail


def test_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIO_TELEGRAM_TOKEN")
    monkeypatch.delenv("SHIO_BACKUP_KEY")
    monkeypatch.delenv("SHIO_HEALTHCHECK_URL")
    checks = do(tmp_path, pinger=FakePinger(url=None))
    by = {c.name: c for c in checks}
    assert not by["SHIO_TELEGRAM_TOKEN"].ok          # telegram.enabled = true
    assert by["SHIO_BACKUP_KEY"].warn and by["SHIO_BACKUP_KEY"].ok
    assert by["SHIO_HEALTHCHECK_URL"].warn and by["SHIO_HEALTHCHECK_URL"].ok
    # нет токена: один ❌ (переменная), строка Telegram — только предупреждение
    assert by["Telegram"].ok and by["Telegram"].warn and "нет токена" in by["Telegram"].detail
    assert [c.name for c in failed(checks)] == ["SHIO_TELEGRAM_TOKEN"]


def test_env_token_missing_telegram_disabled_is_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIO_TELEGRAM_TOKEN")
    s = settings(tmp_path)
    s.telegram.enabled = False
    checks = do(tmp_path, s=s)
    assert failed(checks) == []
    assert {c.name: c for c in checks}["SHIO_TELEGRAM_TOKEN"].warn


def test_invalid_backup_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIO_BACKUP_KEY", "not base64!!")
    bad = failed(do(tmp_path))
    assert [c.name for c in bad] == ["SHIO_BACKUP_KEY"]
    assert "not base64" not in format_checks(bad)


def test_chat_id_missing_when_enabled(tmp_path):
    s = settings(tmp_path)
    s.telegram.chat_id = None
    bad = failed(do(tmp_path, s=s))
    assert len(bad) == 1 and "telegram-id" in bad[0].detail


def test_chat_id_example_placeholder_fails(tmp_path):
    s = settings(tmp_path)
    s.telegram.chat_id = 123456789
    bad = failed(do(tmp_path, s=s))
    assert [c.name for c in bad] == ["Telegram chat_id"]
    assert "пример" in bad[0].detail and "telegram-id" in bad[0].detail


def test_db_wal(tmp_path):
    db = next(c for c in do(tmp_path) if c.name == "База данных")
    assert db.ok and "WAL" in db.detail
    assert "каталог создан" in db.detail
    assert (tmp_path / "data" / "shio.db").exists()
    db2 = next(c for c in do(tmp_path) if c.name == "База данных")
    assert "каталог создан" not in db2.detail


def test_db_dir_not_writable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = settings(tmp_path)
    s.db_path = str(blocker / "shio.db")  # родитель — файл, не каталог
    bad = failed(do(tmp_path, s=s))
    assert any(c.name == "База данных" for c in bad)


def test_healthcheck_fail_is_warning(tmp_path):
    checks = do(tmp_path, pinger=FakePinger(ok=False))
    hc = next(c for c in checks if c.name == "Сторож")
    assert hc.ok and hc.warn
    assert "hc-ping.com" in hc.detail


def test_healthcheck_uses_log_not_success_ping(tmp_path):
    p = FakePinger()
    hc = next(c for c in do(tmp_path, pinger=p) if c.name == "Сторож")
    assert p.calls == [(False, "/log")]
    assert hc.ok and not hc.warn and "адрес сторожа доступен" in hc.detail


def test_launchpool_enabled_no_sources(tmp_path):
    checks = do(tmp_path, sources=[])
    lp = [c for c in checks if "Launchpool" in c.name]
    assert len(lp) == 1 and lp[0].warn and "нет источников" in lp[0].detail


def test_telegram_bad_token_hint(tmp_path):
    from aiogram.utils.token import TokenValidationError

    async def getme(token):
        raise TokenValidationError("Token is invalid!")

    bad = failed(do(tmp_path, getme=getme))
    assert len(bad) == 1 and "токен бота неверный, возьмите новый у @BotFather" in bad[0].detail


def test_launchpool_network_hint(tmp_path):
    class Dns:
        exchange = "bybit"

        async def fetch(self):
            raise OSError("dns")

    bad = failed(do(tmp_path, sources=[Dns()]))
    assert "нет доступа в интернет или DNS" in bad[0].detail


def test_launchpool_http_error_hint(tmp_path):
    import aiohttp

    class Http:
        exchange = "okx"

        async def fetch(self):
            raise aiohttp.ClientResponseError(None, (), status=403)

    bad = failed(do(tmp_path, sources=[Http()]))
    assert "сервер ответил ошибкой, возможна блокировка" in bad[0].detail


def test_format_checks():
    text = format_checks([Check("A", True, "ok"), Check("B", True, "hm", warn=True),
                          Check("C", False, "bad")])
    lines = text.splitlines()
    assert lines[0] == "✅ A — ok"
    assert lines[1] == "⚠️ B — hm"
    assert lines[2] == "❌ C — bad"
    assert lines[-1] == "Есть проблемы: 1"


def test_cli_exit_codes(tmp_path, monkeypatch):
    from engine import __main__ as cli

    async def fake_checks(settings, **kw):
        return [Check("A", True, "ok")]

    monkeypatch.setattr(doctor, "run_checks", fake_checks)
    assert cli.main(["doctor"]) == 0

    async def bad_checks(settings, **kw):
        return [Check("A", False, "bad")]

    monkeypatch.setattr(doctor, "run_checks", bad_checks)
    assert cli.main(["doctor"]) == 1


def test_cli_bad_config(tmp_path, capsys):
    from engine import __main__ as cli

    assert cli.main(["--config", str(tmp_path / "missing.yaml"), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "❌ Настройки" in out and "Есть проблемы: 1" in out
