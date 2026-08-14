"""config.py：环境加载、策略 Schema、插件配置测试。"""
import io
import logging
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import (
    PluginsConfig,
    StrategyFile,
    load_env_file,
    load_plugins_config,
    load_strategy_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_valid() -> dict:
    return yaml.safe_load((FIXTURES / "strategy_valid.yaml").read_text(encoding="utf-8"))


class TestEnvFile:
    def test_load_file_no_override(self, tmp_path, monkeypatch):
        env = tmp_path / "test.env"
        env.write_text("FOO=bar\n# comment\nBAR=baz\n")
        monkeypatch.setenv("FOO", "existing")
        load_env_file(env)
        assert os.environ["FOO"] == "existing"
        assert os.environ["BAR"] == "baz"

    def test_missing_file_ok(self, tmp_path):
        load_env_file(tmp_path / "nope.env")


class TestLogging:
    def test_secret_redacted(self):
        from app.core.config import _SecretFilter

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(_SecretFilter(["supersecret"]))
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("redact_test")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("key=supersecret end")
        assert "supersecret" not in stream.getvalue()
        assert "***" in stream.getvalue()


class TestStrategyValid:
    def test_valid_fixture_loads(self):
        strategy = load_strategy_file(FIXTURES / "strategy_valid.yaml")
        assert strategy.revision == "strategy-2"
        assert strategy.parent_revision == "strategy-1"
        assert strategy.solana.security.gt_score_min == 75
        assert isinstance(strategy.bsc.security.allowed_pool_models, list)
        assert len(strategy.strategy_hash) == 64

    def test_strategy_hash_stable(self):
        a = load_strategy_file(FIXTURES / "strategy_valid.yaml")
        b = load_strategy_file(FIXTURES / "strategy_valid.yaml")
        assert a.strategy_hash == b.strategy_hash


class TestStrategyRejections:
    def test_unquoted_decimal_rejected(self):
        raw = _load_valid()
        raw["solana"]["collection"]["reserve_usd_floor"] = 25000
        with pytest.raises(ValidationError, match="带引号"):
            StrategyFile.model_validate(raw)

    def test_unknown_key_rejected(self):
        raw = _load_valid()
        raw["solana"]["collection"]["bogus_key"] = 1
        with pytest.raises(ValidationError, match="bogus_key"):
            StrategyFile.model_validate(raw)

    def test_dimension_weight_sum(self):
        raw = _load_valid()
        raw["solana"]["scoring"]["dimensions"]["market_quality"]["weight"] = "0.5"
        with pytest.raises(ValidationError, match="维度权重之和"):
            StrategyFile.model_validate(raw)

    def test_feature_direction_reversed(self):
        raw = _load_valid()
        # fdv_liquidity_ratio 是反向特征，good 必须 < bad
        feat = raw["solana"]["scoring"]["dimensions"]["market_quality"]["features"]
        feat["fdv_liquidity_ratio"]["good"] = "500"
        with pytest.raises(ValidationError, match="反向特征"):
            StrategyFile.model_validate(raw)

    def test_feature_good_equals_bad(self):
        raw = _load_valid()
        feat = raw["solana"]["scoring"]["dimensions"]["microstructure"]["features"]
        feat["close_location_10s"]["good"] = feat["close_location_10s"]["bad"]
        with pytest.raises(ValidationError, match="不得相等"):
            StrategyFile.model_validate(raw)

    def test_megafilter_injected_keys_rejected(self):
        raw = _load_valid()
        raw["solana"]["collection"]["query_templates"][0]["query"][
            "reserve_in_usd_min"
        ] = 1000
        with pytest.raises(ValidationError, match="注入参数"):
            StrategyFile.model_validate(raw)

    def test_fixed_four_templates(self):
        raw = _load_valid()
        raw["solana"]["collection"]["query_templates"] = (
            raw["solana"]["collection"]["query_templates"][:3]
        )
        with pytest.raises(ValidationError, match="四个入口"):
            StrategyFile.model_validate(raw)

    def test_gt_score_fixed(self):
        raw = _load_valid()
        raw["solana"]["security"]["gt_score_min"] = 60
        with pytest.raises(ValidationError):
            StrategyFile.model_validate(raw)

    def test_wrong_security_struct(self):
        raw = _load_valid()
        raw["solana"]["security"] = raw["bsc"]["security"]
        with pytest.raises(ValidationError):
            StrategyFile.model_validate(raw)

    def test_g2_enabled_rejected(self):
        raw = _load_valid()
        raw["solana"]["collection"]["websocket"]["g2_enabled"] = True
        with pytest.raises(ValidationError):
            StrategyFile.model_validate(raw)

    def test_duplicate_trusted_assets(self):
        raw = _load_valid()
        assets = raw["solana"]["market_quality"]["trusted_quote_assets"]
        assets.append(dict(assets[0]))
        with pytest.raises(ValidationError, match="地址不得重复"):
            StrategyFile.model_validate(raw)

    def test_fill_deadline_min(self):
        raw = _load_valid()
        raw["solana"]["backtest_model"]["fill_deadline_seconds"] = 100
        with pytest.raises(ValidationError, match="greater_than_equal"):
            StrategyFile.model_validate(raw)

    def test_bad_revision_pattern(self):
        raw = _load_valid()
        raw["revision"] = "v1"
        with pytest.raises(ValidationError):
            StrategyFile.model_validate(raw)

    def test_missing_change_reason(self):
        raw = _load_valid()
        raw["change_reason"] = ""
        with pytest.raises(ValidationError):
            StrategyFile.model_validate(raw)


class TestPluginsConfig:
    def test_load_missing_ok(self, tmp_path):
        cfg = load_plugins_config(tmp_path / "nope.yaml")
        assert cfg.enabled == []

    def test_load_and_duplicates(self, tmp_path):
        path = tmp_path / "plugins.yaml"
        path.write_text("enabled: [trade]\n", encoding="utf-8")
        assert load_plugins_config(path).enabled == ["trade"]

        path.write_text("enabled: [trade, trade]\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="不得重复"):
            load_plugins_config(path)

    def test_unknown_key(self, tmp_path):
        path = tmp_path / "plugins.yaml"
        path.write_text("enabled: []\nbogus: 1\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="bogus"):
            load_plugins_config(path)
