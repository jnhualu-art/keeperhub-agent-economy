"""HealthFactorAgent (src/health_factor_agent.py) 单元测试 — 分级与还款额计算。

这是项目旗舰 agent (已真上链)。决策逻辑是纯函数式的, 重点测:
  1. Aave V3 base 单位归一化 (HF 用 1e18, USD 用 1e8; 无债务时 HF=2^256-1 -> inf)
  2. SAFE / WARN / CRITICAL 分级阈值
  3. PROTECT action 的还款额 = debt * 对应档位比例
"""

import math

import pytest

from health_factor_agent import HF_BASE, DEBT_BASE, HealthFactorAgent, HealthFactorConfig


def make_agent(**cfg) -> HealthFactorAgent:
    defaults = dict(
        agent_name="hfsentinel.test",
        use_keeperhub=False,  # 测试绝不连真链
        warn_hf=1.5,
        critical_hf=1.15,
        target_hf=2.0,
    )
    defaults.update(cfg)
    return HealthFactorAgent(HealthFactorConfig(**defaults))


def feed(agent: HealthFactorAgent, hf: float, debt_usd: float, collateral_usd: float = 20_000.0):
    """把模拟仓位直接灌进 _current_data, 跳过 fetch (不碰网络)。"""
    agent._current_data = {
        "timestamp": 0,
        "live": False,
        "health_factor": hf,
        "collateral_usd": collateral_usd,
        "debt_usd": debt_usd,
    }
    return agent.run_cycle()


# ---------------------------------------------------------------------------
# _normalize: base 单位换算
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_hf_and_usd_units(self):
        agent = make_agent()
        raw = {
            "healthFactor": str(int(1.5 * HF_BASE)),
            "totalCollateralBase": str(int(10_000 * DEBT_BASE)),
            "totalDebtBase": str(int(5_000 * DEBT_BASE)),
        }
        data = agent._normalize(raw, live=True)
        assert data["health_factor"] == pytest.approx(1.5)
        assert data["collateral_usd"] == 10_000.0
        assert data["debt_usd"] == 5_000.0
        assert data["live"] is True

    def test_no_debt_means_infinite_hf(self):
        """Aave 无债务时 healthFactor 返回 2^256-1, 必须归一为 inf 而不是溢出。"""
        agent = make_agent()
        raw = {"healthFactor": str(2**256 - 1), "totalDebtBase": "0"}
        data = agent._normalize(raw, live=False)
        assert data["health_factor"] == float("inf")


# ---------------------------------------------------------------------------
# run_cycle: 分级阈值
# ---------------------------------------------------------------------------

class TestRiskTiers:
    def test_hf_at_or_below_one_is_liquidatable_critical(self):
        result = feed(make_agent(), hf=0.98, debt_usd=1000.0)
        assert result["metrics"]["risk_level"] == "CRITICAL"
        assert "liquidatable" in result["notes"]

    def test_hf_below_critical_threshold(self):
        result = feed(make_agent(), hf=1.10, debt_usd=1000.0)  # 1.10 < 1.15
        assert result["metrics"]["risk_level"] == "CRITICAL"

    def test_hf_below_warn_threshold(self):
        result = feed(make_agent(), hf=1.25, debt_usd=1000.0)  # 1.15 <= 1.25 < 1.5
        assert result["metrics"]["risk_level"] == "WARN"

    def test_hf_at_warn_boundary_is_warn(self):
        result = feed(make_agent(), hf=1.4999, debt_usd=1000.0)
        assert result["metrics"]["risk_level"] == "WARN"

    def test_hf_at_warn_threshold_is_safe(self):
        result = feed(make_agent(), hf=1.5, debt_usd=1000.0)
        assert result["metrics"]["risk_level"] == "SAFE"
        assert result["actions"] == []  # SAFE 不产出 action

    def test_no_debt_emits_no_action(self):
        agent = make_agent()
        agent._current_data = {"timestamp": 0, "live": False, "health_factor": None,
                               "collateral_usd": 100.0, "debt_usd": 0.0}
        result = agent.run_cycle()
        assert result["actions"] == []
        assert "no borrow" in result["notes"]


# ---------------------------------------------------------------------------
# PROTECT action: 还款额与结构
# ---------------------------------------------------------------------------

class TestProtectionAction:
    def test_warn_repays_10_pct_of_debt(self):
        result = feed(make_agent(), hf=1.25, debt_usd=132.30)
        (action,) = result["actions"]
        assert action["type"] == "PROTECT"
        assert action["level"] == "WARN"
        assert action["repay_usd"] == pytest.approx(13.23)  # 132.30 * 0.10
        assert action["repay_asset"] == "USDC"
        assert action["target_hf"] == 2.0

    def test_critical_repays_50_pct_of_debt(self):
        result = feed(make_agent(), hf=0.98, debt_usd=100.0)
        (action,) = result["actions"]
        assert action["level"] == "CRITICAL"
        assert action["repay_usd"] == pytest.approx(50.0)  # 100 * 0.50

    def test_action_rounds_to_cents(self):
        result = feed(make_agent(), hf=1.25, debt_usd=100.0 / 3)
        (action,) = result["actions"]
        assert action["repay_usd"] == round(100.0 / 3 * 0.10, 2)


# ---------------------------------------------------------------------------
# 实测场景回归: audit.jsonl 里那笔真上链交易的前置决策
# ---------------------------------------------------------------------------

class TestRealScenarioRegression:
    def test_sepolia_incident_replay(self):
        """复现 9/1 真实上链场景: HF 1.2471 (WARN) -> 还 13.23 USDC -> HF 1.3856。

        借款 105.84 USDC (13.23 / 0.125 近似 WARN 档), 验证同输入产出同决策。
        """
        agent = make_agent()
        agent._current_data = {
            "timestamp": 0,
            "live": True,
            "health_factor": 1.2471,
            "collateral_usd": 315.75,
            "debt_usd": 132.30,
        }
        result = agent.run_cycle()
        assert result["metrics"]["risk_level"] == "WARN"
        (action,) = result["actions"]
        assert action["repay_usd"] == pytest.approx(13.23)
