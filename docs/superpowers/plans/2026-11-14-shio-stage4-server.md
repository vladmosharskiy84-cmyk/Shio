# Shio stage 4 — Server 24/7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Shio engine 24/7 on a VPS in Docker with clean shutdown, an external watchdog, a self-check command and a Russian step-by-step install guide.

**Architecture:** One Docker Compose service `engine` (the `api` and `caddy` services from spec §6 arrive with stage 3, which is postponed by owner decision: Telegram is the control surface until the strategy is proven). Data (`data/`) is a bind-mounted volume; secrets come from `.env`. Watchdog = heartbeat tick also pings healthchecks.io. `doctor` checks everything live from the server.

**Tech Stack:** Python 3.11+ (image `python:3.12-slim`), Docker Compose v2, ccxt async, aiohttp (already a dependency of ccxt/aiogram), pytest.

**Spec:** docs/superpowers/specs/2026-11-12-shio-design.md (§11 Работа 24/7, §12, §14 п.4, §15)

## Global Constraints
- Secrets only in env vars, never in config.yaml / DB / logs: `SHIO_TELEGRAM_TOKEN`, `SHIO_BACKUP_KEY`, new `SHIO_HEALTHCHECK_URL`.
- User-facing text (CLI output, logs meant for the owner, docs) in Russian.
- Nothing Telegram/network-related may stall the Runner: every network call in a tick is bounded (≤10 s) and its failure is logged, never raised out of the tick.
- Shutdown never closes positions: SIGTERM/SIGINT stops the loop, positions stay hedged, Reconciler checks them at next start.
- Tests run without network (fakes only). Full suite `python -m pytest -q` green after every task.
- Spec §11: VPS 2 GB RAM, ~$5–7/month, country NOT on any trading exchange's restricted list (not Singapore, not USA); `restart: always`; watchdog signal every minute, alert if absent 3 minutes; update = `git pull` + one command (`deploy.sh`).

## Review Focus
1. SIGTERM arriving mid-tick (e.g. during close_pair) — the tick must finish, then the loop exits; a second signal must not corrupt the DB. Test: Task 1 `test_stop_waits_for_current_step`.
2. healthchecks.io unreachable / slow — heartbeat must still be written to DB and Runner not blocked >10 s. Test: Task 1 `test_ping_timeout_does_not_block`.
3. `SHIO_HEALTHCHECK_URL` unset — no pings, one info log, no errors. Test: Task 1 `test_no_url_no_ping`.
4. `doctor` when one exchange is down — reports ❌ for it and continues with the rest; exit code 1. Test: Task 2 `test_doctor_continues_after_failure`.
5. Container restarted with stale `data/` from another version / missing config — clear Russian error, non-zero exit, `restart: always` must not spin silently (logs show reason). Test: Task 3 `test_health_no_db`.

---

### Task 1: Clean shutdown, watchdog ping, log rotation

**Files:**
- Create: `engine/watchdog.py`
- Modify: `engine/runner.py` (heartbeat tick calls pinger), `engine/__main__.py` (`_run`, `_cmd_run`, `_setup_logging`)
- Test: `tests/engine/test_watchdog.py`, `tests/engine/test_runner.py`

**Interfaces:**
- Produces: `class HealthPinger(url: str | None, http_get: Callable[[str, float], Awaitable[int]] | None = None, timeout_s: float = 10.0)` with `async def ping(self, fail: bool = False) -> bool` (GET `url`, or `url + "/fail"` when fail=True; returns True on 2xx; never raises; failures logged at most once per 10 min) and `async def aclose()`. `HealthPinger.from_env()` reads `SHIO_HEALTHCHECK_URL` (stripped; empty → disabled). URL never logged in full (log only host).
- Runner gets optional `pinger=None` kwarg; `_heartbeat` writes DB heartbeat first, then `await pinger.ping()` wrapped in `asyncio.wait_for(..., 10)`.
- `_run` installs SIGTERM and SIGINT handlers via `loop.add_signal_handler` that call `runner.stop()` (loop finishes current step, then exits; existing finally-block cleanup runs). Second signal → log "Повторный сигнал: дождитесь завершения" only.
- `_setup_logging`: `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=5)`; idempotent (no duplicate handlers if called twice).

- [ ] Step 1: tests in `tests/engine/test_watchdog.py`: `test_no_url_no_ping` (url None → ping returns False, fake http never called), `test_ping_ok` (fake returns 200 → True, called with exact url), `test_ping_fail_suffix` (fail=True → url+"/fail"), `test_ping_timeout_does_not_block` (fake sleeps 60 s, timeout_s=0.1 → returns False within 1 s), `test_ping_error_logged_once` (two failures within 10 min → one warning log record; URL path not in log text), `test_from_env_strips`.
- [ ] Step 2: runner tests: `test_heartbeat_pings_after_db_write` (fake pinger records DB heartbeat present at call time), `test_heartbeat_pinger_exception_isolated` (pinger raises → heartbeat still written, step returns), `test_stop_waits_for_current_step` (stop() called from inside a tick → that tick completes, run_forever returns after the step).
- [ ] Step 3: run, see failures; implement; run full suite green.
- [ ] Step 4: `_run` wiring: build `HealthPinger.from_env()`, pass to Runner, `await pinger.aclose()` in finally; signal handlers (guard `NotImplementedError` on platforms without add_signal_handler). Test `test_setup_logging_idempotent` (call twice → one file handler).
- [ ] Step 5: commit `feat(engine): clean shutdown on SIGTERM, healthchecks.io watchdog, log rotation`.

### Task 2: `doctor` self-check command

**Files:**
- Create: `engine/doctor.py`
- Modify: `engine/__main__.py` (subcommand `doctor`)
- Test: `tests/engine/test_doctor.py`

**Interfaces:**
- Consumes: `Settings`, `Collector`'s client factory (`engine.collector.ExchangeClient` / ccxt), `engine.launchpool` sources + `AiohttpJson`, `HealthPinger` (Task 1), `engine.backup.load_key`, `engine.telegram.bot` (getMe via aiogram Bot).
- Produces: `@dataclass Check(name: str, ok: bool, detail: str, warn: bool = False)`; `async def run_checks(settings, *, exchange_factory, telegram_getme, announcement_sources, pinger, db_path) -> list[Check]`; `def format_checks(checks) -> str` (lines `✅ name — detail` / `⚠️` / `❌`, final summary line "Всё в порядке" or "Есть проблемы: N"); CLI `python -m engine doctor` exit 0 if no ❌ else 1.
- Checks, in order, each bounded by 15 s timeout and isolated (exception → ❌ with short Russian detail, never traceback):
  1. config loads; `virtual` mode stated.
  2. env: SHIO_TELEGRAM_TOKEN set (⚠️ if telegram.enabled false and unset; ❌ if enabled and unset), SHIO_BACKUP_KEY valid via load_key (⚠️ if unset), SHIO_HEALTHCHECK_URL (⚠️ if unset). Never print values.
  3. data dir writable + DB opens (Db.init) + journal_mode is wal.
  4. every watched exchange: public `fetch_funding_rates` or ticker for BTC perp → ✅ with count/latency; failure → ❌ "биржа недоступна с этого сервера: <ExcClass>" (geo-block hint if HTTP 403/451).
  5. Telegram getMe → ✅ "@botname"; chat_id set in config (❌ if enabled and missing).
  6. each launchpool source fetch → ✅ with N announcements and first 2 titles (so owner can verify categories are right).
  7. healthcheck ping → ✅/⚠️.
  8. server clock skew vs exchange time (ccxt `fetch_time` on first reachable trading exchange) → ⚠️ if > 2 s.
- [ ] Step 1: tests with fakes: `test_all_ok_exit_zero`, `test_doctor_continues_after_failure` (one exchange raises → ❌ for it, others ✅, exit 1), `test_timeout_is_failure` (fake hangs, timeout patched to 0.1), `test_secrets_never_printed` (token/key/url values absent from output), `test_geo_block_hint` (exception message containing 403 → detail mentions "заблокирована для страны сервера"), `test_launchpool_titles_shown`, `test_clock_skew_warn`.
- [ ] Step 2: fail → implement → green.
- [ ] Step 3: commit `feat(engine): doctor self-check command`.

### Task 3: Docker, deploy script, health command, backup retention

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `deploy.sh`
- Modify: `engine/__main__.py` (subcommand `health`), `engine/runner.py` or `engine/backup.py` (local retention), `.gitignore` (`data/backups/`)
- Test: `tests/engine/test_health.py`, `tests/engine/test_backup.py`, `tests/test_deploy_files.py`

**Interfaces:**
- `python -m engine health`: exit 0 if DB exists and heartbeat age ≤ 180 s; else exit 1 with one Russian line (no DB → "База не найдена: <path>"; stale → "Движок молчит N с"). Used as Docker HEALTHCHECK.
- `engine.backup.prune_local(dir: Path, keep: int = 14) -> int` deletes oldest `*.db.enc` beyond `keep`, returns count; called after every local save.
- Dockerfile: `python:3.12-slim`, non-root user `shio` (uid 1000), `pip install --no-cache-dir -r requirements.txt`, `WORKDIR /app`, `ENV PYTHONUNBUFFERED=1 TZ=UTC`, `CMD ["python","-m","engine","run"]`, `HEALTHCHECK --interval=60s --timeout=10s --start-period=180s CMD python -m engine health`. `STOPSIGNAL SIGTERM`.
- docker-compose.yml: service `engine`, `build: .`, `restart: always`, `env_file: .env`, volumes `./data:/app/data` and `./config.yaml:/app/config.yaml:ro`, `stop_grace_period: 60s`, logging driver json-file max-size 10m max-file 3, `mem_limit: 1g`.
- `.env.example`: the three SHIO_* variables with Russian comments, empty values.
- `deploy.sh` (bash, `set -euo pipefail`): `git pull --ff-only`, `docker compose build`, `docker compose up -d`, wait up to 120 s for container health=healthy printing status, then `docker compose exec engine python -m engine status`; exits non-zero with last 50 log lines if unhealthy. Also `./deploy.sh logs` → `docker compose logs -f --tail 200`, `./deploy.sh doctor` → `docker compose run --rm engine python -m engine doctor`, `./deploy.sh restore <file>` → stop, run restore, start.
- [ ] Step 1: tests: `test_health_fresh` / `test_health_stale` / `test_health_no_db` (exit codes, messages); `test_prune_local_keeps_newest_14`; `tests/test_deploy_files.py`: compose YAML parses and has restart always, env_file .env, stop_grace_period, data volume; Dockerfile contains HEALTHCHECK, non-root USER, CMD run; `.env.example` has the 3 names and no values; `bash -n deploy.sh` passes; `.dockerignore` excludes `data/`, `.env`, `.git`, `.superpowers`.
- [ ] Step 2: fail → implement → green. If docker is available, `docker compose config -q` must pass (skip otherwise).
- [ ] Step 3: commit `feat(deploy): Docker image, compose, deploy.sh, health command, backup retention`.

### Task 4: Russian install guide and owner checklist

**Files:**
- Create: `docs/INSTALL.md`
- Modify: `README.md` (short "Сервер" section linking INSTALL.md)

Content (Russian, numbered, copy-paste commands each in its own block):
1. Что купить: VPS 2 ГБ RAM, Ubuntu 24.04, страна — Германия или Финляндия (Hetzner CX22 ~€4/мес) или Нидерланды; НЕ США и НЕ Сингапур (биржи их ограничивают); Bybit, OKX и Hyperliquid должны открываться с сервера — это подтверждает `doctor`.
2. Вход по SSH, создание пользователя, ключ SSH, отключение пароля, `ufw allow OpenSSH && ufw enable`, автообновления безопасности (`unattended-upgrades`).
3. Установка Docker (официальный скрипт get.docker.com), добавить пользователя в группу docker.
4. `git clone` приватного репо (deploy key или токен с правом только чтения), `cp .env.example .env`, заполнить.
5. Telegram: @BotFather, токен в .env, `docker compose run --rm engine python -m engine telegram-id`, chat_id в config.yaml, `enabled: true`.
6. Ключ бэкапа: команда генерации, ОБЯЗАТЕЛЬНО сохранить копию вне сервера (менеджер паролей).
7. healthchecks.io: регистрация, check с period 1 min / grace 3 min, интеграция Telegram, URL в .env.
8. `./deploy.sh doctor` — что значит каждый ❌ и что делать.
9. `./deploy.sh` — запуск; проверка `/status` в Telegram.
10. Ежедневно/еженедельно: что смотреть; обновление (`./deploy.sh`); восстановление из бэкапа; где логи.
11. Виртуальный режим 2 недели: что записывать, критерий перехода к этапу 5.
- [ ] Step 1: write; verify every command referenced exists (`python -m engine --help` lists doctor, health, telegram-id, restore; deploy.sh subcommands match).
- [ ] Step 2: commit `docs: Russian server install guide`.
