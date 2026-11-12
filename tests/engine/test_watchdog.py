import asyncio
import logging
import time

from engine.watchdog import HealthPinger

URL = "https://hc-ping.com/secret-uuid-123"


def run(coro):
    return asyncio.run(coro)


class FakeHttp:
    def __init__(self, status=200, delay=0.0, exc=None):
        self.status, self.delay, self.exc = status, delay, exc
        self.calls = []

    async def __call__(self, url, timeout):
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.status


def test_no_url_no_ping():
    http = FakeHttp()
    assert run(HealthPinger(None, http).ping()) is False
    assert http.calls == []


def test_ping_ok():
    http = FakeHttp(200)
    assert run(HealthPinger(URL, http).ping()) is True
    assert http.calls == [URL]


def test_ping_fail_suffix():
    http = FakeHttp(200)
    assert run(HealthPinger(URL, http).ping(fail=True)) is True
    assert http.calls == [URL + "/fail"]


def test_ping_non_2xx_false():
    assert run(HealthPinger(URL, FakeHttp(500)).ping()) is False


def test_ping_timeout_does_not_block():
    http = FakeHttp(delay=60)
    start = time.monotonic()
    assert run(HealthPinger(URL, http, timeout_s=0.1).ping()) is False
    assert time.monotonic() - start < 1


def test_ping_error_logged_once(caplog):
    http = FakeHttp(exc=OSError("boom " + URL))
    pinger = HealthPinger(URL, http)
    with caplog.at_level(logging.WARNING):
        assert run(pinger.ping()) is False
        assert run(pinger.ping()) is False
    recs = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(recs) == 1
    assert "secret-uuid-123" not in caplog.text
    assert "hc-ping.com" in caplog.text


def test_from_env_strips(monkeypatch):
    monkeypatch.setenv("SHIO_HEALTHCHECK_URL", "  " + URL + " \n")
    assert HealthPinger.from_env().url == URL
    monkeypatch.setenv("SHIO_HEALTHCHECK_URL", "   ")
    assert HealthPinger.from_env().url is None
    monkeypatch.delenv("SHIO_HEALTHCHECK_URL")
    assert HealthPinger.from_env().url is None


def test_fail_suffix_keeps_query():
    http = FakeHttp(200)
    run(HealthPinger("https://hc-ping.com/uuid?rid=5", http).ping(fail=True))
    assert http.calls == ["https://hc-ping.com/uuid/fail?rid=5"]


def test_ping_custom_suffix():
    http = FakeHttp(200)
    assert run(HealthPinger("https://hc-ping.com/uuid?rid=5", http).ping(suffix="/log")) is True
    assert http.calls == ["https://hc-ping.com/uuid/log?rid=5"]
