"""Настройки движка: загрузка и проверка config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Literal

import yaml


class ConfigError(Exception):
    """Ошибка в настройках (текст — на русском)."""


@dataclass
class Limits:
    max_leverage: float = 3
    max_pair_share: float = 0.5
    max_positions: int = 3
    aggressive_share: float = 0.3
    min_auto_apr: float = 20
    daily_loss_pause: float = 0.05
    warn_buffer: float = 0.40
    act_buffer: float = 0.20
    max_drift: float = 0.01
    proposal_ttl_s: int = 900
    max_slippage: float = 0.001
    leg_retries: int = 3


@dataclass
class ExchangeCfg:
    name: str
    trade: bool
    watch: bool
    spot_taker: float
    perp_taker: float


@dataclass
class Settings:
    capital_usd: float
    mode: Literal["semi", "auto"]
    virtual: bool
    limits: Limits
    exchanges: dict[str, ExchangeCfg]
    paper_balances: dict[str, float]
    aggressive_apr: float
    min_perp_volume_usd: float
    min_spot_volume_usd: float
    max_price_diff_pct: float
    hold_days: float
    db_path: str = "data/shio.db"
    collect_interval_s: int = 60
    guard_interval_s: int = 30
    history_periods: int = 21
    history_check_top: int = 15


def _num(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label}: ожидается число, получено {value!r}")
    return value


def _bool(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label}: ожидается true или false, получено {value!r}")
    return value


def _require(raw: dict, key: str, label: str):
    if key not in raw or raw[key] is None:
        raise ConfigError(f"Не задан параметр «{label}»")
    return raw[key]


def _load_limits(raw: dict) -> Limits:
    known = {f.name for f in fields(Limits)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Неизвестные лимиты: {', '.join(sorted(unknown))}")
    base = Limits()
    values = {}
    for k, v in raw.items():
        _num(v, f"limits.{k}")
        values[k] = type(getattr(base, k))(v) if isinstance(getattr(base, k), int) else v
    lim = Limits(**values)

    if not 1 <= lim.max_leverage <= 10:
        raise ConfigError(f"limits.max_leverage: плечо должно быть от 1 до 10, получено {lim.max_leverage}")
    for name in ("max_pair_share", "aggressive_share", "daily_loss_pause",
                 "warn_buffer", "act_buffer", "max_drift"):
        v = getattr(lim, name)
        if not 0 < v <= 1:
            raise ConfigError(f"limits.{name}: доля должна быть в (0; 1], получено {v}")
    if lim.act_buffer >= lim.warn_buffer:
        raise ConfigError("limits.act_buffer должен быть меньше warn_buffer")
    for name in ("max_positions", "proposal_ttl_s", "leg_retries"):
        if getattr(lim, name) < 1:
            raise ConfigError(f"limits.{name}: должно быть не меньше 1")
    if lim.min_auto_apr < 0 or lim.max_slippage < 0:
        raise ConfigError("limits.min_auto_apr и max_slippage не могут быть отрицательными")
    return lim


def _load_exchanges(raw: dict) -> dict[str, ExchangeCfg]:
    out: dict[str, ExchangeCfg] = {}
    for name, ec in raw.items():
        ec = ec or {}
        watch = _bool(ec.get("enabled", False), f"exchanges.{name}.enabled")
        trade = _bool(ec.get("trade", False), f"exchanges.{name}.trade")
        if watch or trade:
            spot = _num(_require(ec, "spot_taker", f"exchanges.{name}.spot_taker"), f"exchanges.{name}.spot_taker")
            perp = _num(_require(ec, "perp_taker", f"exchanges.{name}.perp_taker"), f"exchanges.{name}.perp_taker")
        else:
            spot = perp = 0.0
        if not (0 <= spot < 0.05 and 0 <= perp < 0.05):
            raise ConfigError(f"exchanges.{name}: комиссия должна быть в долях от 0 до 0.05")
        if trade and not watch:
            raise ConfigError(f"Биржа {name}: торговать можно только на наблюдаемой бирже (enabled: true)")
        out[name] = ExchangeCfg(name, trade, watch, float(spot), float(perp))
    return out


def load_settings(path: str) -> Settings:
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"Не удалось прочитать файл настроек {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Некорректный YAML в {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("Файл настроек пуст или имеет неверный формат")

    eng = raw.get("engine") or {}
    if not isinstance(eng, dict):
        raise ConfigError("Секция engine должна быть словарём")

    mode = eng.get("mode", "semi")
    if mode not in ("semi", "auto"):
        raise ConfigError(f"engine.mode: неизвестный режим {mode!r}, допустимо semi или auto")
    virtual = _bool(eng.get("virtual", True), "engine.virtual")

    capital = _num(_require(raw, "capital_usd", "capital_usd"), "capital_usd")
    if capital <= 0:
        raise ConfigError("capital_usd: капитал должен быть больше нуля")

    limits = _load_limits(eng.get("limits") or {})
    exchanges = _load_exchanges(raw.get("exchanges") or {})

    paper = {str(k): float(_num(v, f"engine.paper_balances.{k}"))
             for k, v in (eng.get("paper_balances") or {}).items()}
    for name, bal in paper.items():
        if name not in exchanges or not exchanges[name].trade:
            raise ConfigError(f"engine.paper_balances: биржа {name} не торговая, баланс на ней задавать нельзя")
        if bal < 0:
            raise ConfigError(f"engine.paper_balances.{name}: баланс не может быть отрицательным")
    if virtual:
        total = sum(paper.values())
        if abs(total - capital) > 1e-9:
            raise ConfigError(
                f"При виртуальной торговле капитал ({capital}) должен равняться сумме "
                f"виртуальных балансов ({total})")

    aggressive_apr = _num(eng.get("aggressive_apr", 80), "engine.aggressive_apr")
    if aggressive_apr <= 0:
        raise ConfigError("engine.aggressive_apr: порог должен быть больше нуля")

    ints = {}
    for key, default in (("collect_interval_s", 60), ("guard_interval_s", 30)):
        v = _num(eng.get(key, default), f"engine.{key}")
        if v < 1:
            raise ConfigError(f"engine.{key}: интервал должен быть не меньше 1 секунды")
        ints[key] = int(v)
    for key, default in (("history_periods", 21), ("history_check_top", 15)):
        v = _num(raw.get(key, default), key)
        if v < 1:
            raise ConfigError(f"{key}: должно быть не меньше 1")
        ints[key] = int(v)

    return Settings(
        capital_usd=float(capital),
        mode=mode,
        virtual=virtual,
        limits=limits,
        exchanges=exchanges,
        paper_balances=paper,
        aggressive_apr=float(aggressive_apr),
        min_perp_volume_usd=float(_num(_require(raw, "min_perp_volume_usd", "min_perp_volume_usd"), "min_perp_volume_usd")),
        min_spot_volume_usd=float(_num(_require(raw, "min_spot_volume_usd", "min_spot_volume_usd"), "min_spot_volume_usd")),
        max_price_diff_pct=float(_num(_require(raw, "max_price_diff_pct", "max_price_diff_pct"), "max_price_diff_pct")),
        hold_days=float(_num(_require(raw, "hold_days", "hold_days"), "hold_days")),
        db_path=str(eng.get("db_path", "data/shio.db")),
        **ints,
    )
