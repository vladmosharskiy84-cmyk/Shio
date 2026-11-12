# Shio — Этап 2: Telegram-бот. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Владелец получает уведомления Shio в Telegram, подтверждает и отклоняет предложения кнопками, управляет ботом командами и каждый день получает сводку и зашифрованную резервную копию базы. Отдельно бот сообщает о новых Launchpool, Launchpad и Jumpstart.

**Architecture:** Telegram работает внутри процесса `python -m engine run` как ещё одна asyncio-задача рядом с `Runner`. Входящие кнопки и команды не трогают торговлю напрямую. Они только пишут строки в таблицу `commands`, как CLI, а `Runner` их исполняет. Вся логика — чистые функции, проверяемые без сети: форматирование, обработчики, уведомитель через протокол `TelegramApi`. Привязка к aiogram сосредоточена в одном тонком файле.

**Tech Stack:** Python 3.11+, aiogram 3.x, cryptography (AES-256-GCM), sqlite3, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-12-shio-design.md` (разделы 2 «Launchpool», 5, 8 «Telegram», 10, 11 «Резервные копии»).

## Global Constraints

- Бот отвечает только на `chat_id` владельца. Остальные сообщения игнорируются: не отвечаем и ничего не исполняем.
- Закрытие позиции и «Стоп всё» требуют подтверждения второй кнопкой.
- Предложение связки приходит с кнопками «Открыть» / «Пропустить» и действует 15 минут (`limits.proposal_ttl_s`).
- Команды: `/status`, `/positions`, `/pause`, `/resume`, `/stop` (с подтверждением), плюс `/help`.
- Ежедневно: зашифрованная (AES-256-GCM) резервная копия базы файлом в Telegram. Восстановление одной командой `python -m engine restore <файл>`.
- Секреты (токен бота, ключ шифрования копий) берутся только из переменных окружения `SHIO_TELEGRAM_TOKEN` и `SHIO_BACKUP_KEY`, никогда из `config.yaml` или базы.
- Если Telegram не настроен или недоступен, двигатель работает дальше с `LogNotifier` и пишет понятное предупреждение.
- Весь текст для пользователя — на русском. Время в сообщениях — UTC.
- Сеть в тестах не используется: Telegram и биржи подменяются подделками.

## Review Focus

1. **Telegram недоступен или отвечает ошибкой/таймаутом.** Отправка повторяется до 3 раз, потом ошибка пишется в журнал. Исключение никогда не долетает до `Runner`. Тест в Task 2.
2. **Сообщение или кнопка от постороннего чата.** Команда в базу не пишется, ответа нет, событие `telegram_foreign` в audit (не чаще раза в час на chat_id). Тест в Task 3.
3. **Двойное нажатие «Открыть» или нажатие после истечения 15 минут.** Пишется максимум одна команда `approve`. На повторное нажатие ответ «уже обработано», на истёкшее — «предложение истекло». Тест в Task 3.
4. **Длинный текст** (`/positions` при многих позициях, длинная ошибка): сообщения режутся на части не длиннее 4096 символов. Тест в Task 1.
5. **Одно и то же тревожное сообщение каждые 30 секунд** (`guard_error`, неизвестная позиция при сверке): одинаковый текст уходит в Telegram не чаще раза в 10 минут, при этом в журнал пишется всегда. Тест в Task 2.

---

## Структура файлов

```
engine/
  report.py           # тексты статуса и позиций (общие для CLI и Telegram), split_message
  telegram/
    __init__.py
    api.py            # Protocol TelegramApi + Button, Message
    notifier.py       # TelegramNotifier: уровни, повторы, анти-спам, proposal с кнопками
    handlers.py       # чистые обработчики команд и кнопок → строки в commands
    bot.py            # привязка к aiogram: AiogramApi + polling
  backup.py           # снимок базы, шифрование/расшифровка
  launchpool.py       # наблюдение за анонсами Bybit/OKX
  notifier.py         # + Notifier.proposal(), LogNotifier.proposal()
  runner.py           # + proposal через notifier, отчёт о командах, тики summary/backup/launchpool
  __main__.py         # + telegram в run, команда restore; status через report.py
  config.py           # + секции telegram, backup, launchpool
tests/engine/telegram/…, tests/engine/test_report.py, test_backup.py, test_launchpool.py
```

---

### Task 1: Настройки и общие тексты отчётов

**Files:**
- Create: `engine/report.py`, `tests/engine/test_report.py`
- Modify: `engine/config.py`, `config.yaml`, `engine/__main__.py` (перенести `build_status`/`format_status` в `report.py`, CLI использует их оттуда), `requirements.txt` (+ `aiogram>=3.4,<4`, `cryptography>=42`), `tests/engine/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass TelegramCfg(enabled: bool=False, chat_id: int|None=None, daily_summary_hour_utc: int=18, dedupe_s: int=600)`
  - `@dataclass BackupCfg(enabled: bool=True, hour_utc: int=3)`
  - `@dataclass LaunchpoolCfg(enabled: bool=True, interval_s: int=1800, exchanges: list[str]=["bybit","okx"])`
  - `Settings` получает поля `telegram: TelegramCfg`, `backup: BackupCfg`, `launchpool: LaunchpoolCfg` со значениями по умолчанию: старые `config.yaml` без этих секций загружаются.
  - `engine/report.py`: `build_status(db, settings, now_ms) -> dict` и `format_status(st: dict) -> str` (переносятся из `__main__.py` без изменения поведения); `format_positions(db, settings, now_ms) -> str` (позиции open/opening/closing: монета, тип, биржи, статус, получено funding, реализованный P&L, запас до ликвидации по perp-ногам через `guardian.leg_buffer`); `split_message(text: str, limit: int = 4096) -> list[str]` (режет по переносу строки, а слишком длинную строку — по символам; каждая часть ≤ limit; склейка частей с `\n` восстанавливает текст).

Проверки конфига: `telegram.enabled: true` без `chat_id` → ConfigError; часы вне 0–23 → ConfigError; `dedupe_s`/`interval_s` < 60 → ConfigError. В `config.yaml` по умолчанию `telegram.enabled: false` с комментарием, как получить `chat_id`.

- [ ] **Step 1: Тесты** — `test_old_config_without_new_sections_loads` (telegram.enabled False, backup.hour_utc 3, launchpool.interval_s 1800); `test_telegram_enabled_requires_chat_id`; `test_hour_out_of_range_rejected`; `test_split_message_respects_limit` (текст 10 000 символов из строк по 100 → каждая часть ≤ 4096, `"\n".join(parts) == text`); `test_split_message_cuts_overlong_line` (одна строка 9000 символов → 3 части); `test_format_positions_lists_buffers` (позиция с perp short entry 100, mark 120, плечо 3 → в тексте есть монета и «запас»); `test_cli_status_unchanged` (вывод `python -m engine status` на пустой базе совпадает с выводом до переноса — сравнить с `format_status(build_status(...))`).
- [ ] **Step 2:** FAIL.  **Step 3:** Реализовать.  **Step 4:** `python3 -m pytest -q` — всё зелёное.
- [ ] **Step 5:** Commit `feat(engine): telegram/backup/launchpool settings and shared report texts`.

### Task 2: TelegramNotifier

**Files:**
- Create: `engine/telegram/__init__.py`, `engine/telegram/api.py`, `engine/telegram/notifier.py`, `tests/engine/telegram/__init__.py`, `tests/engine/telegram/fakes.py`, `tests/engine/telegram/test_notifier.py`
- Modify: `engine/notifier.py` (протокол + `LogNotifier.proposal`), `engine/runner.py` (`_propose` вызывает `notifier.proposal(p, ttl_min, hold_days)`; после каждой команды — `notifier.send` с результатом), `tests/engine/test_runner.py`

**Interfaces:**
- Produces:
  - `engine/telegram/api.py`: `@dataclass Button(text: str, data: str)`; `class TelegramApi(Protocol)`: `async send_message(chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> int` (возвращает message_id); `async edit_message(chat_id: int, message_id: int, text: str) -> None`; `async answer_callback(callback_id: str, text: str) -> None`; `async send_document(chat_id: int, filename: str, data: bytes, caption: str) -> None`.
  - `engine/notifier.py`: `Notifier` получает `async proposal(p: Proposal, ttl_min: int, hold_days: float) -> None`; `format_proposal(p, ttl_min, hold_days) -> str` (текст нынешнего `_propose`, но без подсказки про CLI); `LogNotifier.proposal` отправляет `format_proposal(...)` плюс подсказку `python -m engine approve <id>` через `send`.
  - `engine/telegram/notifier.py`: `class TelegramNotifier(api: TelegramApi, chat_id: int, db: Db, dedupe_s: int, clock=time.time, retries: int = 3, retry_delay_s: float = 1.0)`. `send(text, level)` всегда пишет в журнал, как `LogNotifier`. В Telegram уходит текст с префиксом по уровню: info — без префикса, warn — «⚠️ », alert — «🚨 ». Длинный текст режется `split_message`. Одинаковый (уровень, текст) не отправляется повторно в течение `dedupe_s`; отметка хранится в памяти. `proposal(...)` отправляет `format_proposal` с кнопками `[[Button("Открыть", f"approve:{id}"), Button("Пропустить", f"reject:{id}")]]` и запоминает `kv["tg_msg:proposal:<id>"] = message_id`. `send_document(...)` проксирует к api. Любое исключение api повторяется до `retries` раз с паузой `retry_delay_s`, потом `db.audit("telegram_error", ...)` и логирование. Наружу исключение не выходит.
  - `tests/engine/telegram/fakes.py`: `FakeTelegramApi` — записывает отправки, умеет падать заданное число раз.
- Runner: после выполнения команды `notifier.send(f"Команда «{type}»: {result}", "info")`, при ошибке — тот же текст с уровнем "warn".

- [ ] **Step 1: Тесты** — `test_levels_prefix`; `test_long_text_split` (Review Focus 4 со стороны отправки); `test_dedupe_same_text_within_window` (два одинаковых alert за 5 минут → 1 сообщение, в audit «notify» — 2 записи; через 11 минут → второе сообщение; Review Focus 5); `test_retries_then_gives_up_without_raising` (api падает 5 раз → 3 попытки, `telegram_error` в audit, исключения нет; Review Focus 1); `test_recovers_after_one_failure`; `test_proposal_has_buttons_and_stores_message_id`; `test_runner_proposal_goes_through_notifier_proposal` (в `test_runner.py`: FakeNotifier записывает вызов `proposal`); `test_runner_reports_command_result`.
- [ ] **Step 2:** FAIL.  **Step 3:** Реализовать (`retry_delay_s=0` в тестах).  **Step 4:** PASS, весь набор.
- [ ] **Step 5:** Commit `feat(telegram): notifier with levels, retries, dedupe and proposal buttons`.

### Task 3: Обработчики команд и кнопок

**Files:**
- Create: `engine/telegram/handlers.py`, `tests/engine/telegram/test_handlers.py`

**Interfaces:**
- Consumes: `Db`, `Settings`, `report.format_status/build_status/format_positions`, `TelegramApi`, `Button`.
- Produces:
  - `@dataclass Reply(text: str, buttons: list[list[Button]] | None = None)`
  - `handle_message(text: str, chat_id: int, db, settings, now_ms: int) -> Reply | None` — `None` для чужого chat_id (Review Focus 2: `db.audit("telegram_foreign", {"chat_id"})` не чаще раза в час на chat_id, отметка в kv). Команды: `/start` и `/help` — список команд; `/status` — `format_status(build_status(...))`; `/positions` — `format_positions(...)`; `/pause`, `/resume` — `db.add_command(...)` и ответ «Принято, бот выполнит в течение нескольких секунд»; `/stop` — ответ с кнопками `[[Button("Да, закрыть всё", "stop:confirm"), Button("Отмена", "cancel")]]`; `/close <id>` — проверить, что позиция в open/closing, и ответить кнопками `close:confirm:<id>` / `cancel`. Неизвестная команда — подсказка `/help`.
  - `handle_callback(data: str, chat_id: int, db, settings, now_ms: int) -> Reply | None` — `None` для чужого chat_id (с той же записью в audit). `approve:<id>` / `reject:<id>`: предложение не найдено → «предложение не найдено»; `expires_at <= now` → «предложение истекло»; статус не pending или в kv стоит отметка `tg_done:proposal:<id>` → «уже обработано»; иначе `add_command`, отметка в kv, ответ «Принято: открываю…» / «Пропущено». `stop:confirm` → `add_command("stop_all")`; `close:confirm:<id>` → `add_command("close", id)`; `cancel` → «Отменено». Повторное подтверждение того же stop/close в течение 60 с команду не дублирует.

- [ ] **Step 1: Тесты** — `test_foreign_chat_ignored_and_audited_once_per_hour` (Review Focus 2); `test_status_and_positions_texts`; `test_pause_resume_write_commands`; `test_stop_requires_confirmation` (`/stop` без нажатия не пишет команду; `stop:confirm` пишет ровно одну `stop_all`); `test_close_requires_confirmation`; `test_close_unknown_position_rejected`; `test_approve_double_tap_writes_one_command` (Review Focus 3); `test_approve_after_expiry_rejected` (Review Focus 3); `test_reject_writes_reject`; `test_unknown_command_hints_help`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(telegram): owner-only command and button handlers`.

### Task 4: Привязка к aiogram и запуск в `run`

**Files:**
- Create: `engine/telegram/bot.py`, `tests/engine/telegram/test_bot.py`
- Modify: `engine/__main__.py` (`_run`), `README.md` (раздел «Telegram: подключение» на русском: создать бота у @BotFather, `export SHIO_TELEGRAM_TOKEN=...`, узнать `chat_id` командой `python -m engine telegram-id`, вписать его в `config.yaml`, включить `telegram.enabled`)

**Interfaces:**
- Consumes: `TelegramApi`, `handle_message`, `handle_callback`, `TelegramNotifier`.
- Produces:
  - `class AiogramApi(TelegramApi)(bot: aiogram.Bot)` — `send_message` с `InlineKeyboardMarkup`, `edit_message`, `answer_callback`, `send_document` через `BufferedInputFile`.
  - `async run_telegram(token: str, settings, db, notifier: TelegramNotifier, stop: asyncio.Event) -> None` — aiogram `Dispatcher` с двумя хендлерами: сообщения → `handle_message`, callback → `handle_callback` (+ `answer_callback`; при `Reply` с текстом редактирует сообщение с кнопками, чтобы их убрать). Polling с автоматическими повторами при сетевых ошибках. Выход по `stop`.
  - `build_notifier(settings, db) -> tuple[Notifier, str | None]`: если `telegram.enabled` и есть `SHIO_TELEGRAM_TOKEN`, возвращает `(TelegramNotifier(AiogramApi(...)), token)`, иначе `(LogNotifier(db), None)` и пишет предупреждение «Telegram не настроен: …» с причиной (Review Focus 1, нет токена).
- Как узнать `chat_id`: `python -m engine telegram-id` запускает бота на 2 минуты и печатает в консоль chat_id первого написавшего. В Telegram при этом ничего не отвечает, так что чужих защита не ослабляется.
- `_run`: `notifier, token = build_notifier(...)`; Runner получает этот notifier; если есть token, `run_telegram` запускается отдельной задачей. Ctrl+C останавливает обе задачи. Падение задачи Telegram пишется в журнал и не останавливает Runner.

- [ ] **Step 1: Тесты (без сети)** — `test_build_notifier_without_token_falls_back` (нет переменной окружения → LogNotifier и предупреждение в журнале); `test_build_notifier_disabled`; `test_build_notifier_with_token` (подменить `AiogramApi` — проверить, что вернулся TelegramNotifier с нужным chat_id); `test_dispatch_routes_message_and_callback` (вызвать внутренние функции-маршрутизаторы `bot.py` с поддельными объектами aiogram-сообщения/колбэка: вызываются нужные обработчики, кнопки убираются после нажатия); `test_telegram_task_failure_does_not_stop_runner` (задача Telegram бросает исключение → `_run` продолжает, ошибка в журнале).
- [ ] **Step 2–4:** FAIL → реализовать → PASS. Импорт aiogram только в `bot.py`.
- [ ] **Step 5: Ручная проверка (у владельца)** — после `export SHIO_TELEGRAM_TOKEN=…` и заполнения `chat_id` команда `/status` в Telegram отвечает. В этом контейнере api.telegram.org может быть недоступен; тогда шаг выполняет владелец по README.
- [ ] **Step 6:** Commit `feat(telegram): aiogram bot wiring in engine run`.

### Task 5: Ежедневная сводка и зашифрованная резервная копия

**Files:**
- Create: `engine/backup.py`, `tests/engine/test_backup.py`
- Modify: `engine/runner.py` (тик раз в минуту: сводка и копия по часам UTC), `engine/__main__.py` (команда `restore`), `README.md` (раздел «Резервные копии»), `tests/engine/test_runner.py`

**Interfaces:**
- Produces:
  - `engine/backup.py`: `snapshot_bytes(db: Db) -> bytes` (sqlite3 backup API во временный файл → байты; база не блокируется); `encrypt(data: bytes, key: bytes) -> bytes` (AES-256-GCM, формат `b"SHIO1" + nonce(12) + ciphertext+tag`); `decrypt(blob: bytes, key: bytes) -> bytes` (неверный ключ или повреждение → `BackupError` с русским текстом); `load_key() -> bytes | None` (из `SHIO_BACKUP_KEY` в base64, ровно 32 байта, иначе `BackupError`); `restore(blob: bytes, key: bytes, db_path: str) -> str` (старый файл переименовывается в `<db_path>.before-restore-<ts>`, записывается расшифрованная база, проверяется `PRAGMA integrity_check` == "ok"; возвращает путь старой копии).
  - Runner: `tick_daily()` раз в минуту. В час `telegram.daily_summary_hour_utc` один раз за UTC-день → `notifier.send(сводка)`: капитал, P&L за день, funding за день (`db.funding_total(начало дня)`), открытые позиции (кол-во), ожидающие предложения. В час `backup.hour_utc` один раз за день (если `backup.enabled` и ключ есть) → `notifier.send_document("shio-YYYY-MM-DD.db.enc", encrypt(snapshot_bytes(db)), "Резервная копия Shio за …")`. Отметки дня — в kv. Ключа нет → одно предупреждение в день.
  - `Notifier` получает `async send_document(filename, data, caption) -> None`. `LogNotifier` сохраняет файл в `data/backups/` и пишет путь в журнал.
  - CLI: `python -m engine restore <файл>` — бот должен быть остановлен (heartbeat старше 3 минут, иначе отказ с текстом), ключ из окружения, вывод — путь старой базы.

- [ ] **Step 1: Тесты** — `test_encrypt_decrypt_roundtrip`; `test_wrong_key_raises_backup_error`; `test_tampered_blob_raises`; `test_load_key_validates_length`; `test_restore_replaces_db_and_keeps_old` (после restore позиции из копии читаются, старый файл существует); `test_daily_summary_once_per_day` (часы сдвигаются через 18:00 дважды в одни сутки → одна сводка, на следующий день — ещё одна); `test_backup_sent_once_per_day_with_key`; `test_backup_skipped_without_key_warns_once`; `test_log_notifier_saves_backup_file`; `test_restore_refuses_while_bot_alive`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): daily summary and encrypted backups with restore`.

### Task 6: Уведомления о Launchpool / Launchpad / Jumpstart

**Files:**
- Create: `engine/launchpool.py`, `tests/engine/test_launchpool.py`
- Modify: `engine/runner.py` (тик раз в `launchpool.interval_s`), `tests/engine/test_runner.py`

**Interfaces:**
- Produces:
  - `@dataclass Announcement(exchange: str, id: str, title: str, url: str, published_ms: int)`
  - `class AnnouncementSource(Protocol)`: `async fetch() -> list[Announcement]`.
  - `BybitAnnouncements(http)` — `GET https://api.bybit.com/v5/announcements/index?locale=en-US&type=new_crypto&limit=20` (поля `list[].title`, `url`, `publishTime`; id = url). `OkxAnnouncements(http)` — `GET https://www.okx.com/api/v5/support/announcements?annType=announcements-new-listings` (поля `data[].details[].title`, `url`, `pTime`; id = url). `http` — объект с `async get_json(url) -> dict` (реализация на `aiohttp`, который ставится вместе с aiogram).
  - `async check_announcements(sources, db, notifier, now_ms) -> list[Announcement]` — оставляет заголовки, содержащие (без учёта регистра) «launchpool», «launchpad», «jumpstart», «megadrop»; уже показанные (kv `lp_seen`, хранить последние 500 id) пропускает. При первом запуске всё найденное помечается как показанное без уведомлений, чтобы не завалить старыми анонсами. Новые → `notifier.send(f"Новый пул на {exchange}: {title}\n{url}", "info")`. Ошибка одного источника → `db.audit("launchpool_error")`, остальные источники проверяются.

- [ ] **Step 1: Тесты (поддельный http)** — `test_filters_keywords`; `test_first_run_marks_seen_without_notify`; `test_new_item_notified_once`; `test_source_error_isolated`; `test_seen_list_capped_at_500`; `test_runner_launchpool_tick_respects_interval_and_disabled`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): launchpool/launchpad/jumpstart announcements`.
