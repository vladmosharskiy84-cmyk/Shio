import copy
from pathlib import Path

import pytest
import yaml

from engine.config import ConfigError, Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
BASE = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def write(tmp_path, mutate=None) -> str:
    cfg = copy.deepcopy(BASE)
    if mutate:
        mutate(cfg)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return str(p)


def test_defaults_loaded():
    s = load_settings(str(ROOT / "config.yaml"))
    assert isinstance(s, Settings)
    assert s.mode == "semi"
    assert s.virtual is True
    assert s.limits.max_leverage == 3
    assert [n for n, e in s.exchanges.items() if e.trade] == ["bybit", "okx", "hyperliquid"]
    assert sorted(n for n, e in s.exchanges.items() if e.watch) == sorted(
        ["binance", "bybit", "okx", "bitget", "gate", "hyperliquid", "whitebit", "mexc"])
    assert s.exchanges["hyperliquid"].spot_taker == 0.0007
    assert s.exchanges["gate"].spot_taker == 0.002
    assert s.paper_balances == {"bybit": 170, "okx": 170, "hyperliquid": 160}
    assert s.aggressive_apr == 80
    assert s.db_path == "data/shio.db"
    assert s.capital_usd == 500


@pytest.mark.parametrize("lev", [0, 11])
def test_rejects_leverage_out_of_range(tmp_path, lev):
    path = write(tmp_path, lambda c: c["engine"]["limits"].update(max_leverage=lev))
    with pytest.raises(ConfigError):
        load_settings(path)


def test_rejects_paper_balance_on_non_trade_exchange(tmp_path):
    def m(c):
        c["engine"]["paper_balances"] = {"bybit": 170, "okx": 170, "binance": 160}
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, m))


def test_rejects_share_above_one(tmp_path):
    path = write(tmp_path, lambda c: c["engine"]["limits"].update(max_pair_share=1.5))
    with pytest.raises(ConfigError):
        load_settings(path)


def test_capital_equals_sum_of_paper_balances_when_virtual(tmp_path):
    def m(c):
        c["capital_usd"] = 500
        c["engine"]["paper_balances"] = {"bybit": 200, "okx": 100, "hyperliquid": 100}
    with pytest.raises(ConfigError, match="капитал"):
        load_settings(write(tmp_path, m))


def test_capital_not_checked_when_not_virtual(tmp_path):
    def m(c):
        c["engine"]["virtual"] = False
        c["capital_usd"] = 999
    assert load_settings(write(tmp_path, m)).capital_usd == 999


def test_rejects_bad_mode(tmp_path):
    with pytest.raises(ConfigError, match="режим"):
        load_settings(write(tmp_path, lambda c: c["engine"].update(mode="turbo")))


def test_fractional_leverage_preserved(tmp_path):
    path = write(tmp_path, lambda c: c["engine"]["limits"].update(max_leverage=2.5))
    assert load_settings(path).limits.max_leverage == 2.5


def test_rejects_leverage_above_ten_fractional(tmp_path):
    path = write(tmp_path, lambda c: c["engine"]["limits"].update(max_leverage=10.9))
    with pytest.raises(ConfigError):
        load_settings(path)


def test_old_config_without_new_sections_loads(tmp_path):
    def m(c):
        for k in ("telegram", "backup", "launchpool"):
            c.pop(k, None)
    s = load_settings(write(tmp_path, m))
    assert s.telegram.enabled is False and s.telegram.chat_id is None
    assert s.backup.hour_utc == 3
    assert s.launchpool.interval_s == 1800 and s.launchpool.exchanges == ["bybit", "okx"]


def test_telegram_enabled_requires_chat_id(tmp_path):
    with pytest.raises(ConfigError, match="chat_id"):
        load_settings(write(tmp_path, lambda c: c.update(telegram={"enabled": True})))
    s = load_settings(write(tmp_path, lambda c: c.update(telegram={"enabled": True, "chat_id": -1001234})))
    assert s.telegram.chat_id == -1001234


@pytest.mark.parametrize("section,key", [("telegram", "daily_summary_hour_utc"), ("backup", "hour_utc")])
@pytest.mark.parametrize("hour", [-1, 24])
def test_hour_out_of_range_rejected(tmp_path, section, key, hour):
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, lambda c: c.update({section: {key: hour}})))


@pytest.mark.parametrize("section,key", [("telegram", "dedupe_s"), ("launchpool", "interval_s")])
def test_interval_below_minute_rejected(tmp_path, section, key):
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, lambda c: c.update({section: {key: 59}})))


def test_default_config_telegram_disabled():
    s = load_settings(str(ROOT / "config.yaml"))
    assert s.telegram.enabled is False and s.telegram.dedupe_s == 600


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("where", ["capital", "limit", "taker"])
def test_rejects_non_finite_numbers(tmp_path, value, where):
    def m(c):
        if where == "capital":
            c["capital_usd"] = value
        elif where == "limit":
            c["engine"]["limits"]["max_pair_share"] = value
        else:
            c["exchanges"]["bybit"]["perp_taker"] = value
    with pytest.raises(ConfigError, match="конечное число"):
        load_settings(write(tmp_path, m))


# --- config.local.yaml: настройки владельца поверх config.yaml ---
def write_local(tmp_path, text):
    p = tmp_path / "config.local.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_local_overrides_deep_merge(tmp_path):
    path = write(tmp_path)
    write_local(tmp_path, "telegram:\n  enabled: true\n  chat_id: 4242\n"
                          "engine:\n  limits:\n    max_leverage: 2\n")
    s = load_settings(path)
    assert s.telegram.enabled is True and s.telegram.chat_id == 4242
    assert s.telegram.dedupe_s == 600  # соседние ключи секции сохранены
    assert s.limits.max_leverage == 2
    assert s.mode == "semi" and s.virtual is True  # остальное из config.yaml


def test_local_lists_and_scalars_replace(tmp_path):
    path = write(tmp_path)
    write_local(tmp_path, "launchpool:\n  exchanges: [okx]\n")
    assert load_settings(path).launchpool.exchanges == ["okx"]


def test_local_missing_or_empty_is_ignored(tmp_path):
    path = write(tmp_path)
    base = load_settings(path)
    write_local(tmp_path, "")
    assert load_settings(path) == base


def test_validation_runs_on_merged_result(tmp_path):
    path = write(tmp_path)
    write_local(tmp_path, "telegram:\n  enabled: true\n")
    with pytest.raises(ConfigError, match="chat_id") as e:
        load_settings(path)
    assert "config.local.yaml" in str(e.value)


def test_local_bad_yaml_names_file(tmp_path):
    path = write(tmp_path)
    write_local(tmp_path, "telegram: [\n")
    with pytest.raises(ConfigError, match="config.local.yaml"):
        load_settings(path)


def test_local_not_a_dict_names_file(tmp_path):
    path = write(tmp_path)
    write_local(tmp_path, "- 1\n- 2\n")
    with pytest.raises(ConfigError, match="config.local.yaml"):
        load_settings(path)


def test_local_is_directory_names_file(tmp_path):
    path = write(tmp_path)
    (tmp_path / "config.local.yaml").mkdir()
    with pytest.raises(ConfigError, match="config.local.yaml"):
        load_settings(path)


def test_error_without_local_names_main_file(tmp_path):
    path = write(tmp_path, lambda c: c.update(capital_usd=-1))
    with pytest.raises(ConfigError) as e:
        load_settings(path)
    assert "config.yaml" in str(e.value) and "config.local.yaml" not in str(e.value)


def test_local_example_is_valid_override(tmp_path):
    path = write(tmp_path)
    write_local(tmp_path, (ROOT / "config.local.yaml.example").read_text(encoding="utf-8"))
    s = load_settings(path)
    assert s.telegram.enabled is True and isinstance(s.telegram.chat_id, int)
