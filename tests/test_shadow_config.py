import json
import os


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config():
    with open(os.path.join(REPO, "user_config.json"), encoding="utf-8") as file:
        return json.load(file)


def test_intraday_strategy_is_shadow_only_by_default():
    cfg = config()
    assert cfg["broker"]["execution_provider"] == "toss"   # v8.0: 한국투자증권 제거
    assert cfg["broker"]["market_data_provider"] == "toss"
    assert cfg["broker"]["execution_mode"] == "shadow"
    assert cfg["intraday_strategy"]["enabled"] is True
    assert cfg["intraday_strategy"]["live_enabled"] is False
    assert cfg["intraday_strategy"]["min_shadow_sessions"] >= 20


def test_intraday_weights_sum_to_one():
    weights = config()["intraday_strategy"]["features"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_no_leverage_symbols_in_operational_rotation_universe():
    us = {item["symbol"] for item in config()["momentum_rotation"]["us"]["universe"]}
    assert not us & {"TQQQ", "SOXL", "UPRO", "TECL", "FNGU", "NVDL"}
