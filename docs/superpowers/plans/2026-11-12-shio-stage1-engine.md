# Shio — Этап 1: двигатель и виртуальная торговля. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Двигатель Shio, который круглосуточно находит связки funding-арбитража, создаёт предложения (полуавтомат) или сам открывает позиции (автомат), ведёт и защищает их и начисляет funding. Всё в виртуальном режиме: реальные рыночные данные, сделки симулируются.

**Architecture:** Пакет `engine/` поверх готового `scanner/` (расчёты и загрузка данных). Модули общаются через SQLite: команды пользователя пишутся в таблицу `commands`, двигатель их забирает. Исполнение идёт через интерфейс `Broker`. На этом этапе есть только `PaperBroker`, реальный брокер появится на этапе 5 за тем же интерфейсом.

**Tech Stack:** Python 3.11+, ccxt (async), sqlite3 (WAL), PyYAML, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-12-shio-design.md` (разделы 4–7, 12, 13).

## Global Constraints

- Только USDT-линейные бессрочные фьючерсы (`BTC/USDT:USDT`) и спот к USDT.
- Режим по умолчанию: виртуальный + полуавтомат. Реального брокера на этом этапе нет.
- Лимиты по умолчанию: плечо до 3x; на связку до 50% капитала; связок одновременно 3; агрессивный профиль до 30% капитала; мин. доходность для Автомата 20% годовых; дневной убыток 5% → пауза.
- Guardian: каждые 30 с; запас до ликвидации ≤ 40% → предупреждение; ≤ 20% → довнести маржу, иначе закрыть обе ноги; перекос номиналов > 1% → выровнять; ожидаемый доход за 24 ч < 0 → закрыть (Автомат) / предложить (Полуавтомат).
- Collector раз в 60 с; предложение действует 15 минут; вторая нога — до 3 попыток, иначе закрыть первую.
- Доход считается по `min(текущая ставка, средняя за 21 выплату)` минус 4 комиссии.
- Торговые биржи по умолчанию: bybit, okx, hyperliquid. Наблюдение: binance, bybit, okx, bitget, gate, hyperliquid, whitebit, mexc.
- Время в базе — UTC, миллисекунды epoch. Деньги — float в USD.
- Весь текст для пользователя (CLI, уведомления, журнал) — на русском.

## Review Focus

1. **Монета пропала из данных или ставка `None` при открытой позиции** (делистинг, сбой биржи): Guardian не падает, использует последнюю известную цену, шлёт предупреждение. Тест в Task 9.
2. **Стакан слишком тонкий для суммы**: Executor не открывает ни одной ноги, связка помечается «недостаточно ликвидности». Тест в Task 7.
3. **Перезапуск посреди открытия** (одна нога исполнена, позиция в статусе `opening`): Reconciler закрывает осиротевшую ногу. Тест в Task 10.
4. **Интервал выплат сменился или время выплаты прыгнуло**: Ledger не начисляет одну выплату дважды. Тест в Task 8.
5. **Цена перепрыгнула ликвидацию между проверками**: позиция помечается `liquidated`, убыток записывается, бот встаёт на паузу. Тест в Task 9.

---

## Структура файлов

```
engine/
  __init__.py
  config.py       # Settings из config.yaml + проверка
  models.py       # dataclass-модели и статусы
  db.py           # схема SQLite и репозиторий
  collector.py    # снимок рынка (обёртка над scanner.exchanges) + стаканы
  finder.py       # кандидаты: расчёт, профиль, проверка глубины
  decider.py      # правила лимитов и режима → Proposal / вход
  broker.py       # Protocol Broker + PaperBroker
  executor.py     # открытие/закрытие пары ног
  ledger.py       # виртуальные выплаты funding, P&L
  guardian.py     # охрана позиций
  reconciler.py   # сверка базы с брокером
  notifier.py     # Protocol Notifier + LogNotifier
  runner.py       # главный цикл asyncio
  __main__.py     # CLI: run / status / proposals / approve / close / stop / pause / resume
scanner/calc.py   # + estimate_fill, liquidation_price, margin_buffer
config.yaml       # расширяется
tests/engine/…    # тесты по модулям; tests/engine/fakes.py — общие подделки
```

---

### Task 1: Настройки

**Files:**
- Create: `engine/__init__.py`, `engine/config.py`, `tests/engine/__init__.py`, `tests/engine/test_config.py`
- Modify: `config.yaml` (новые секции), `requirements.txt` (+ `pytest-asyncio>=0.23`)

**Interfaces:**
- Produces:
  - `@dataclass Limits(max_leverage: float=3, max_pair_share: float=0.5, max_positions: int=3, aggressive_share: float=0.3, min_auto_apr: float=20, daily_loss_pause: float=0.05, warn_buffer: float=0.40, act_buffer: float=0.20, max_drift: float=0.01, proposal_ttl_s: int=900, max_slippage: float=0.001, leg_retries: int=3)`
  - `@dataclass ExchangeCfg(name: str, trade: bool, watch: bool, spot_taker: float, perp_taker: float)`
  - `@dataclass Settings(capital_usd: float, mode: Literal["semi","auto"], virtual: bool, limits: Limits, exchanges: dict[str, ExchangeCfg], paper_balances: dict[str, float], aggressive_apr: float, collect_interval_s: int=60, guard_interval_s: int=30, history_periods: int=21, history_check_top: int=15, min_perp_volume_usd: float, min_spot_volume_usd: float, max_price_diff_pct: float, hold_days: float, db_path: str)`
  - `load_settings(path: str) -> Settings`, `ConfigError(Exception)`

Значения по умолчанию в `config.yaml`: mode `semi`, virtual `true`, `paper_balances: {bybit: 170, okx: 170, hyperliquid: 160}`, `aggressive_apr: 80`. Комиссии taker: bybit 0.001/0.00055, okx 0.001/0.0005, hyperliquid 0/0.00045, binance 0.001/0.0005, bitget 0.001/0.0006, gate 0.002/0.0005, whitebit 0.001/0.00055, mexc 0.0005/0.0002. `trade: true` только у bybit, okx, hyperliquid.

- [ ] **Step 1: Тесты** — `test_defaults_loaded` (mode == "semi", virtual is True, limits.max_leverage == 3, `[n for n,e in s.exchanges.items() if e.trade] == ["bybit","okx","hyperliquid"]`); `test_rejects_leverage_out_of_range` (max_leverage 0 и 11 → ConfigError); `test_rejects_paper_balance_on_non_trade_exchange`; `test_rejects_share_above_one` (max_pair_share 1.5 → ConfigError); `test_capital_equals_sum_of_paper_balances_when_virtual` (500 ≠ 400 → ConfigError с текстом на русском).
- [ ] **Step 2:** `pytest tests/engine/test_config.py -v` → FAIL (нет модуля).
- [ ] **Step 3:** Реализовать `load_settings` (yaml.safe_load → dataclasses, проверки с сообщениями на русском). Старые ключи сканера в `config.yaml` сохранить: `main.py` продолжает работать.
- [ ] **Step 4:** `pytest -q` → всё зелёное, включая старые тесты.
- [ ] **Step 5:** Commit `feat(engine): settings with limits and exchange roles`.

### Task 2: Модели и база

**Files:**
- Create: `engine/models.py`, `engine/db.py`, `tests/engine/test_db.py`

**Interfaces:**
- Produces (`engine/models.py`):
  - `PositionStatus = Literal["opening","open","closing","closed","failed","liquidated"]`
  - `@dataclass Leg(id: int|None, position_id: int|None, exchange: str, symbol: str, kind: Literal["spot","perp"], side: Literal["long","short"], amount: float, entry_price: float, leverage: float, margin: float, fees_paid: float=0.0, funding_received: float=0.0, last_funding_ts: int|None=None, pending_rate: float|None=None, pending_ts: int|None=None, open: bool=True)`
  - `@dataclass Position(id: int|None, kind: Literal["spot-perp","perp-perp"], base: str, profile: Literal["calm","aggressive"], status: PositionStatus, capital: float, opened_at: int, closed_at: int|None=None, realized_pnl: float=0.0, close_reason: str|None=None, legs: list[Leg]=field(default_factory=list))`
  - `@dataclass Proposal(id: int|None, created_at: int, expires_at: int, base: str, kind: str, profile: str, legs_plan: list[dict], capital: float, expected_apr: float, expected_profit: float, status: Literal["pending","approved","rejected","expired","executed","failed"])`
  - `@dataclass Command(id: int|None, created_at: int, type: Literal["approve","reject","close","stop_all","pause","resume"], arg: str|None, status: Literal["new","done","error"]="new", result: str|None=None)`
- Produces (`engine/db.py`): `class Db(path: str)`: `init()`; `save_position(p) -> int` (с ногами); `update_position(p)`; `get_position(id) -> Position`; `positions(status: set[str]|None=None) -> list[Position]`; `save_proposal(p) -> int`; `update_proposal(p)`; `pending_proposals(now_ms) -> list[Proposal]`; `add_command(type, arg=None) -> int`; `take_new_commands() -> list[Command]`; `finish_command(id, status, result)`; `add_funding(leg_id, ts, rate, amount)`; `funding_total(since_ms=None) -> float`; `audit(event: str, detail: dict)`; `heartbeat(now_ms)`; `get_kv(key, default=None)` / `set_kv(key, value)` (JSON).

Таблицы: `positions`, `legs`, `proposals`, `commands`, `funding_payments` (UNIQUE(leg_id, ts)), `audit_log`, `kv`. `PRAGMA journal_mode=WAL` в `init()`.

- [ ] **Step 1: Тесты** — `test_position_roundtrip_with_legs`; `test_wal_enabled` (`PRAGMA journal_mode` == "wal"); `test_init_idempotent` (дважды `init()` без ошибки); `test_commands_taken_once` (второй `take_new_commands()` пуст); `test_funding_unique_per_leg_and_ts` (повторный `add_funding` с тем же (leg, ts) не меняет `funding_total`); `test_pending_proposals_excludes_expired`.
- [ ] **Step 2:** Тесты падают.
- [ ] **Step 3:** Реализовать на `sqlite3`, `legs_plan` хранить в JSON. Повтор `add_funding` — `INSERT OR IGNORE`.
- [ ] **Step 4:** Тесты зелёные.
- [ ] **Step 5:** Commit `feat(engine): sqlite models and repository`.

### Task 3: Расчёты исполнения и риска

**Files:**
- Modify: `scanner/calc.py`
- Test: `tests/test_calc.py`

**Interfaces:**
- Produces:
  - `estimate_fill(levels: list[list[float]], notional_usd: float) -> tuple[float, float, float]`: идёт по уровням `[price, amount]`, возвращает `(avg_price, filled_usd, slippage)`, где `slippage = |avg - best| / best`.
  - `liquidation_price(entry: float, side: str, leverage: float, mmr: float=0.005) -> float`: short `entry*(1+1/lev-mmr)`, long `entry*(1-1/lev+mmr)`.
  - `margin_buffer(entry: float, mark: float, liq: float) -> float`: доля оставшегося пути от входа до ликвидации, `(liq-mark)/(liq-entry)`, ограничена [0, 1].

- [ ] **Step 1: Тесты** — `estimate_fill([[100,1],[101,1]], 150)` → avg ≈ 100.33, filled 150, slippage ≈ 0.0033; при книге на $50 и сумме $150 → filled 50; `liquidation_price(100,"short",3)` == 132.83; `liquidation_price(100,"long",3)` ≈ 67.17; `margin_buffer(100,100,132.83)` == 1.0; `margin_buffer(100,126.26,132.83)` ≈ 0.2; цена за ликвидацией → 0.0.
- [ ] **Step 2:** FAIL.  **Step 3:** Реализовать.  **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(calc): fill estimation and liquidation math`.

### Task 4: Collector и Finder

**Files:**
- Create: `engine/collector.py`, `engine/finder.py`, `tests/engine/fakes.py`, `tests/engine/test_finder.py`
- Modify: `scanner/exchanges.py` (+ `ExchangeClient.order_book(symbol, limit=20) -> dict`, `ExchangeClient.amount_to_precision(symbol, amount) -> float`)

**Interfaces:**
- Consumes: `scanner.exchanges.ExchangeClient`, `scanner.engine.find_opportunities`, `scanner.calc.*`, `Settings`.
- Produces:
  - `@dataclass MarketSnapshot(ts: int, perps: list[PerpInfo], spots: list[SpotInfo], ok: dict[str, bool])`, плюс `perp(exchange, symbol) -> PerpInfo|None`.
  - `class Collector(settings, client_factory=ExchangeClient)`: `async collect() -> MarketSnapshot` (все `watch`-биржи, история для лучших `history_check_top`); `async order_book(exchange, symbol) -> dict`; `async round_amount(exchange, symbol, amount) -> float`; `async close()`.
  - `@dataclass Candidate(op: Opportunity, profile: Literal["calm","aggressive"], legs_plan: list[dict], capital: float, liquidity_ok: bool)`. Элемент `legs_plan`: `{"exchange","symbol","kind","side"}`.
  - `classify_profile(op: Opportunity, aggressive_apr: float) -> str`: aggressive, если `apr_net_capital >= aggressive_apr`, или нет истории, или история < 40% текущей ставки; иначе calm.
  - `async find_candidates(snapshot, collector, settings, capital_for: Callable[[str], float]) -> list[Candidate]`: только связки, где **все** ноги на `trade`-биржах; проверка глубины через `estimate_fill` для каждой ноги (`slippage <= limits.max_slippage` и `filled == нужный номинал`); сортировка по `apr_net_capital`.

`tests/engine/fakes.py`: `FakeExchange` (переносится из `tests/test_engine.py` и дополняется `fetch_order_book`), `FakeClientFactory`, `FakeNotifier`, `ScriptedBroker` (Task 7).

- [ ] **Step 1: Тесты** — `test_only_trade_exchange_legs` (связка с ногой на gate отсутствует, хотя у gate лучшая ставка); `test_thin_book_marks_liquidity_false`; `test_profile_spike_is_aggressive`; `test_profile_stable_moderate_is_calm`; `test_failed_exchange_marked_not_ok` (биржа бросает исключение → `snapshot.ok[name] is False`, остальные есть).
- [ ] **Step 2:** FAIL.  **Step 3:** Реализовать.  **Step 4:** `pytest -q` PASS.
- [ ] **Step 5:** Commit `feat(engine): collector and candidate finder with depth check`.

### Task 5: Decider

**Files:**
- Create: `engine/decider.py`, `tests/engine/test_decider.py`

**Interfaces:**
- Consumes: `Candidate`, `Db`, `Settings`.
- Produces:
  - `@dataclass Decision(action: Literal["propose","enter","skip"], candidate: Candidate, reason: str)`
  - `decide(candidates: list[Candidate], open_positions: list[Position], pending: list[Proposal], equity: float, day_pnl: float, paused: bool, settings: Settings) -> list[Decision]`
  - `capital_for_pair(equity: float, settings) -> float` = `equity * limits.max_pair_share`.

Правила по порядку (первое сработавшее → `skip` с причиной на русском): пауза; дневной убыток ≤ −`daily_loss_pause`·equity; уже открыто `max_positions`; монета уже в позиции или в активном предложении; `liquidity_ok` False; сумма агрессивных + новая > `aggressive_share`·equity; `apr_net_capital` ≤ 0. Дальше mode `auto` и `apr_net_capital >= min_auto_apr` → `enter`, иначе `propose`. Слотов на одном шаге не больше, чем `max_positions − открытые`.

- [ ] **Step 1: Тесты** — `test_semi_mode_proposes`; `test_auto_mode_enters_above_min_apr`; `test_auto_mode_proposes_below_min_apr`; `test_respects_max_positions`; `test_no_duplicate_base`; `test_aggressive_share_cap` (агрессивных уже на 30% → skip); `test_daily_loss_pauses` (day_pnl = −26 при equity 500 → все skip, reason содержит «дневной»).
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): decision rules and limits`.

### Task 6: Broker и PaperBroker

**Files:**
- Create: `engine/broker.py`, `tests/engine/test_paper_broker.py`

**Interfaces:**
- Consumes: `Collector.order_book`, `Collector.round_amount`, `estimate_fill`, `Settings` (комиссии).
- Produces:
  - `@dataclass Fill(exchange: str, symbol: str, side: Literal["buy","sell"], requested: float, filled: float, avg_price: float, fee: float)`
  - `class Broker(Protocol)`: `async round_amount(exchange, symbol, amount) -> float`; `async market_ioc(exchange, symbol, kind, side, amount, max_slippage) -> Fill`; `async set_leverage(exchange, symbol, leverage) -> None`; `async free_balance(exchange) -> float`; `async add_margin(exchange, symbol, usd) -> bool`; `async open_amounts(exchange) -> dict[str, float]` (символ → объём со знаком, для Reconciler).
  - `class PaperBroker(Broker)(collector, settings, db)`: балансы в `kv["paper_balances"]` (старт из `settings.paper_balances`); исполнение по живому стакану, `filled` обрезается там, где проскальзывание превысило бы `max_slippage`; комиссия = notional × taker (spot/perp); резервирует маржу `notional/leverage` для perp и полную стоимость для spot; `open_amounts` из своих записей в `kv["paper_holdings"]`.

- [ ] **Step 1: Тесты** — `test_buy_walks_asks_and_charges_taker`; `test_ioc_partial_when_slippage_exceeded` (filled < requested); `test_balance_decreases_by_margin_plus_fee`; `test_insufficient_balance_fills_zero`; `test_open_amounts_tracks_holdings` (после short 2 → `{"X/USDT:USDT": -2.0}`); `test_state_survives_new_instance` (новый PaperBroker с той же базой видит те же балансы).
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): broker protocol and paper broker`.

### Task 7: Executor

**Files:**
- Create: `engine/executor.py`, `tests/engine/test_executor.py`
- Modify: `tests/engine/fakes.py` (+ `ScriptedBroker`: заранее заданные результаты `market_ioc` по вызовам)

**Interfaces:**
- Consumes: `Broker`, `Db`, `Notifier`, `Candidate.legs_plan`, `Settings.limits`.
- Produces:
  - `async open_pair(plan: list[dict], capital: float, base: str, kind: str, profile: str, broker, db, notifier, settings) -> Position` — итоговый статус `open` или `failed`.
  - `async close_pair(position: Position, reason: str, broker, db, notifier, settings) -> Position` — итоговый статус `closed`, `realized_pnl` = funding − все комиссии + P&L цены ног.

Алгоритм `open_pair`: номинал на ногу = `spot_perp_notional` / `perp_perp_notional` из `scanner/calc.py` с `limits.max_leverage`; объём = `round_amount` от `номинал/цена`, общий для обеих ног (меньший из округлённых); позиция пишется в базу со статусом `opening` **до** первого ордера; плечо ставится до ордеров; первой идёт нога с меньшим объёмом книги; вторая нога — до `leg_retries` попыток на остаток; если вторая нога исполнена частично — первая уменьшается встречным ордером до того же объёма; если вторая не исполнена совсем — первая закрывается целиком, статус `failed`, уведомление. Первая нога с `filled == 0` → `failed` без второй. `close_pair` в обратном порядке, с теми же повторами.

- [ ] **Step 1: Тесты** — `test_opens_both_legs_equal_amount`; `test_status_opening_written_before_first_order` (ScriptedBroker проверяет базу при первом вызове); `test_second_leg_fails_three_times_unwinds_first` (итог `failed`, у брокера нулевые позиции, уведомление содержит «закрыта первая нога»); `test_partial_second_leg_shrinks_first`; `test_thin_book_opens_nothing` (Review Focus 2: первая нога filled 0 → ни одного открытого объёма); `test_close_pair_records_realized_pnl`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): two-leg executor with unwind on failure`.

### Task 8: Ledger (виртуальные выплаты funding)

**Files:**
- Create: `engine/ledger.py`, `tests/engine/test_ledger.py`

**Interfaces:**
- Consumes: `MarketSnapshot`, `Db`, `Position/Leg`.
- Produces:
  - `accrue(snapshot: MarketSnapshot, positions: list[Position], db, now_ms: int) -> list[tuple[Leg, float]]` — начисленные выплаты.
  - `equity(db, broker_balances: dict[str, float], positions, snapshot) -> float`; `day_pnl(db, now_ms) -> float`.

Алгоритм: для каждой открытой perp-ноги хранить `pending_rate` и `pending_ts` из снимка (`funding_rate`, `next_funding_ts`). Когда `now_ms >= pending_ts`, начислить `amount = −sign(side) × amount × mark × pending_rate` (short получает при ставке > 0) через `db.add_funding(leg.id, pending_ts, …)` (уникальность защищает от двойного начисления), затем взять новые значения. Если `next_funding_ts` в снимке нет — начислений нет, `pending` не меняется.

- [ ] **Step 1: Тесты** — `test_short_receives_positive_funding` (amount 10, mark 2, rate 0.001 → +0.02); `test_long_pays_positive_funding`; `test_no_double_count_on_same_ts` (два вызова accrue после одного ts → одна запись); `test_interval_change_no_double_count` (Review Focus 4: ts прыгает с 8ч на 1ч сетку — каждая выплата по одному разу); `test_missing_next_ts_skips`; `test_day_pnl_sums_today_only`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): paper funding ledger and pnl`.

### Task 9: Guardian

**Files:**
- Create: `engine/guardian.py`, `tests/engine/test_guardian.py`

**Interfaces:**
- Consumes: `liquidation_price`, `margin_buffer`, `Broker`, `close_pair`, `Notifier`, `MarketSnapshot`, `Settings`.
- Produces:
  - `@dataclass GuardAction(position_id: int, kind: Literal["ok","warn","topup","close","propose_close","rebalance","liquidated","stale"], detail: str)`
  - `async guard(positions, snapshot, broker, db, notifier, settings, last_marks: dict[tuple[str,str], float]) -> list[GuardAction]`

Правила для каждой открытой позиции: цена ноги из снимка, иначе из `last_marks` (действие `stale` + предупреждение; Review Focus 1); если цены нет совсем — только `stale`, без падения. Буфер ≤ `act_buffer` → `add_margin` на сумму, возвращающую буфер к 50%; не удалось → `close_pair` (reason «угроза ликвидации»). Буфер ≤ `warn_buffer` → `warn` (одно уведомление на позицию в час, отметка в kv). Цена за ликвидацией (буфер 0) → статус `liquidated`, нога закрыта по цене ликвидации с потерей маржи, `kv["paused"]=True`, уведомление (Review Focus 5). Перекос номиналов > `max_drift` → `rebalance` (докупить или допродать меньшую ногу через брокера). Ожидаемый доход за 24 ч (текущие ставки ног, нормированные на час, × 24 × номинал) < 0 → `close` в режиме auto, `propose_close` в semi (уведомление с предложением).

- [ ] **Step 1: Тесты** — `test_healthy_position_ok`; `test_warn_at_40_percent_once_per_hour`; `test_topup_at_20_percent`; `test_close_when_topup_fails`; `test_gap_past_liquidation_marks_liquidated_and_pauses`; `test_missing_symbol_uses_last_mark_and_warns`; `test_reversal_closes_in_auto_proposes_in_semi`; `test_drift_triggers_rebalance`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): guardian with margin, drift and reversal rules`.

### Task 10: Reconciler

**Files:**
- Create: `engine/reconciler.py`, `tests/engine/test_reconciler.py`

**Interfaces:**
- Consumes: `Broker.open_amounts`, `Db`, `Notifier`, `close_pair`.
- Produces: `async reconcile(broker, db, notifier, settings) -> list[str]` — список расхождений текстом на русском.

Правила: позиции `opening`/`closing` после перезапуска — если у брокера открыты объёмы по их ногам, а пара неполная → закрыть оставшееся, статус `failed`; если пара полная → статус `open`. Объём у брокера без позиции в базе → сообщение «неизвестная позиция», без автоматических действий. Позиция `open` в базе, а у брокера нуля → статус `closed`, reason «не найдена на бирже», уведомление.

- [ ] **Step 1: Тесты** — `test_orphan_leg_after_restart_closed` (Review Focus 3); `test_complete_opening_pair_promoted_to_open`; `test_unknown_exchange_position_reported_not_touched`; `test_missing_on_exchange_marked_closed`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5:** Commit `feat(engine): startup reconciliation`.

### Task 11: Runner, Notifier и CLI

**Files:**
- Create: `engine/notifier.py`, `engine/runner.py`, `engine/__main__.py`, `tests/engine/test_runner.py`
- Modify: `README.md` (раздел «Двигатель: запуск и команды»)

**Interfaces:**
- Consumes: всё выше.
- Produces:
  - `class Notifier(Protocol)`: `async send(text: str, level: Literal["info","warn","alert"]="info") -> None`; `LogNotifier` пишет в лог и `audit_log`.
  - `class Runner(settings, collector, broker, db, notifier, clock=time.time)`: `async tick_collect()` (collect → accrue → find → decide → создать Proposal / вызвать open_pair → истечение предложений по TTL); `async tick_guard()`; `async tick_commands()` (approve/reject/close/stop_all/pause/resume); `async run_forever()` (reconcile при старте; collect раз в `collect_interval_s`, guard раз в `guard_interval_s`, commands раз в 2 с, `db.heartbeat` каждую минуту; исключение в тике пишется в журнал и не останавливает цикл).
  - CLI `python -m engine <cmd>`: `run`, `status` (equity, P&L за день и всего, позиции с буфером), `proposals`, `approve <id>`, `reject <id>`, `close <position_id>`, `stop` (спрашивает «да»), `pause`, `resume`. Команды кроме `run` и `status` только пишут в `commands`.

- [ ] **Step 1: Тесты (на подделках, без сети)** — `test_full_cycle_semi` (tick_collect → есть pending Proposal → `approve` → tick_commands → позиция open → снимок с прошедшей выплатой → tick_collect начисляет funding → `status` показывает прибыль > 0); `test_auto_mode_enters_without_approval`; `test_proposal_expires_after_15_min` (часы сдвинуты на 901 с → статус expired); `test_stop_all_closes_everything`; `test_tick_exception_does_not_stop_loop` (collector бросает один раз → следующий тик работает, ошибка в audit_log); `test_heartbeat_written`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS (`pytest -q` — весь набор).
- [ ] **Step 5: Ручная проверка** — `python -m engine run` на реальных данных на 10 минут (у владельца или на сервере с доступом к биржам): в журнале видны снимки всех доступных бирж и как минимум одно предложение или причины пропуска; `python -m engine status` работает.
- [ ] **Step 6:** Commit `feat(engine): runner loop, notifier and CLI`.
