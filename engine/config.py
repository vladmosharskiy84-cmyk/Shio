"""Настройки движка: загрузка и проверка config.yaml (+ config.local.yaml владельца)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

import yaml


class ConfigError(Exception):
    """Ошибка в настройках (текст — на русском)."""


@dataclass
class Limits:
    max_leverage: float = 3.0
    max_pair_share: float = 0.5
    max_positions: int = 3
    aggressive_share: float = 0.3
    min_auto_apr: float = 20.0
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
class TelegramCfg:
    enabled: bool = False
    chat_id: int | None = None
    daily_summary_hour_utc: int = 18
    dedupe_s: int = 600


@dataclass
class BackupCfg:
    enabled: bool = True
    hour_utc: int = 3


@dataclass
class LaunchpoolCfg:
    enabled: bool = True
    interval_s: int = 1800
    exchanges: list[str] = field(default_factory=lambda: ["bybit", "okx"])


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
    min_net_apr: float = 0.0  # мин. доходность связки, % годовых: ниже — не предлагаем и не входим
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    backup: BackupCfg = field(default_factory=BackupCfg)
    launchpool: LaunchpoolCfg = field(default_factory=LaunchpoolCfg)


def _num(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label}: ожидается число, получено {value!r}")
    if not math.isfinite(value):
        raise ConfigError(f"{label}: ожидается конечное число, получено {value!r}")
    return value


def _bool(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label}: ожидается true или false, получено {value!r}")
    return value


def _require(raw: dict, key: str, label: str):
    if key not in raw or raw[key] is None:
        raise ConfigError(f"Не задан параметр «{label}»")
    return raw[key]


_INT_LIMITS = {"max_positions", "proposal_ttl_s", "leg_retries"}


def _load_limits(raw: dict) -> Limits:
    known = {f.name for f in fields(Limits)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Неизвестные лимиты: {', '.join(sorted(unknown))}")
    base = Limits()
    values = {}
    for k, v in raw.items():
        _num(v, f"limits.{k}")
        values[k] = int(v) if k in _INT_LIMITS else v
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


def _section(raw: dict, name: str, cls):
    sec = raw.get(name)
    if sec is None:
        return {}
    if not isinstance(sec, dict):
        raise ConfigError(f"Секция {name} должна быть словарём")
    unknown = set(sec) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"{name}: неизвестные параметры: {', '.join(sorted(unknown))}")
    return sec


def _hour(value, label: str) -> int:
    v = _num(value, label)
    if v != int(v) or not 0 <= v <= 23:
        raise ConfigError(f"{label}: час (UTC) должен быть целым числом от 0 до 23, получено {value!r}")
    return int(v)


def _seconds(value, label: str) -> int:
    v = _num(value, label)
    if v < 60:
        raise ConfigError(f"{label}: интервал должен быть не меньше 60 секунд, получено {value!r}")
    return int(v)


def _load_telegram(raw: dict) -> TelegramCfg:
    sec = _section(raw, "telegram", TelegramCfg)
    enabled = _bool(sec.get("enabled", False), "telegram.enabled")
    chat_id = sec.get("chat_id")
    if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, int)):
        raise ConfigError(f"telegram.chat_id: ожидается целое число, получено {chat_id!r}")
    if enabled and chat_id is None:
        raise ConfigError("telegram.enabled: true требует telegram.chat_id (номер вашего чата в Telegram)")
    return TelegramCfg(
        enabled=enabled, chat_id=chat_id,
        daily_summary_hour_utc=_hour(sec.get("daily_summary_hour_utc", 18), "telegram.daily_summary_hour_utc"),
        dedupe_s=_seconds(sec.get("dedupe_s", 600), "telegram.dedupe_s"))


def _load_backup(raw: dict) -> BackupCfg:
    sec = _section(raw, "backup", BackupCfg)
    return BackupCfg(enabled=_bool(sec.get("enabled", True), "backup.enabled"),
                     hour_utc=_hour(sec.get("hour_utc", 3), "backup.hour_utc"))


def _load_launchpool(raw: dict) -> LaunchpoolCfg:
    sec = _section(raw, "launchpool", LaunchpoolCfg)
    names = sec.get("exchanges", ["bybit", "okx"])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError("launchpool.exchanges: ожидается список названий бирж")
    return LaunchpoolCfg(enabled=_bool(sec.get("enabled", True), "launchpool.enabled"),
                         interval_s=_seconds(sec.get("interval_s", 1800), "launchpool.interval_s"),
                         exchanges=list(names))


def local_path_for(path: str | Path) -> Path:
    """config.yaml -> config.local.yaml рядом с ним (настройки владельца, не в git)."""
    p = Path(path)
    return p.with_name(f"{p.stem}.local{p.suffix}")


def _read_yaml(path: str | Path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"Не удалось прочитать файл настроек {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Некорректный YAML в {path}: {e}") from e


def _deep_merge(base: dict, over: dict) -> dict:
    """Словари сливаются рекурсивно; числа, строки и списки из over заменяют значения base."""
    out = dict(base)
    for key, value in over.items():
        if value is None and isinstance(out.get(key), dict):
            continue  # пустой заголовок секции («engine:» без ключей) ничего не меняет
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: str) -> Settings:
    """config.yaml, поверх него — необязательный config.local.yaml; проверка — по итогу."""
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"Файл настроек {path} пуст или имеет неверный формат")
    where = str(path)
    local = local_path_for(path)
    if local.exists():
        over = _read_yaml(local)
        if over is not None:
            if not isinstance(over, dict):
                raise ConfigError(f"Файл настроек {local} должен содержать секции "
                                  f"(ключ: значение), а не список или текст")
            raw = _deep_merge(raw, over)
            where = f"{path} + {local}"
    try:
        return _build_settings(raw)
    except ConfigError as e:
        raise ConfigError(f"{where}: {e}") from None


def _build_settings(raw: dict) -> Settings:

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

    min_net_apr = _num(raw.get("min_net_apr", 0.0), "min_net_apr")
    if min_net_apr < 0:
        raise ConfigError("min_net_apr: порог доходности не может быть отрицательным")

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
        min_net_apr=float(min_net_apr),
        telegram=_load_telegram(raw),
        backup=_load_backup(raw),
        launchpool=_load_launchpool(raw),
        **ints,
    )
