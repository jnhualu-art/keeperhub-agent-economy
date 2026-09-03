"""CapitalEfficiencyAgent 单元测试。

覆盖两条线:
  1. compute_max_borrow 纯函数 —— 风控模型的全部边界 (无抵押 / 无债务 /
     HF 已贴安全线 / 链上 cap 更紧 / safety 折扣)
  2. Agent 决策 —— 用假 client 注入链上数据, 验证「该借 / 不该借」的判定
     与产出 action 的字段

全部不打网络、不读 .env、不碰真实审计日志。
"""

import pytest

from capital_efficiency_agent import (
    CapitalEfficiencyAgent,
    CapitalEfficiencyConfig,
    _normalize_hf,
    compute_max_borrow,
)
from keeperhub_client import MCPError


# ── 假 client: 只实现 agent 需要的那一个方法 ────────────────────

class FakeClient:
    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error
        self.calls = []

    def get_user_account_data(self, user, network=None):
        self.calls.append((user, network))
        if self._error:
            raise self._error
        return self._data


def _account(coll, debt, avail, threshold_bps=8250):
    """构造 Aave get-user-account-data 的返回 (base currency 8 位小数)"""
    return {
        "totalCollateralBase": str(int(coll * 1e8)),
        "totalDebtBase": str(int(debt * 1e8)),
        "availableBorrowsBase": str(int(avail * 1e8)),
        "currentLiquidationThreshold": str(threshold_bps),
        "ltv": "8000",
        "healthFactor": str(int(coll * (threshold_bps / 1e4) / debt * 1e18)) if debt else str(2 ** 256 - 1),
    }


# ── _normalize_hf ──────────────────────────────────────────────

def test_normalize_hf_converts_from_1e18():
    # 1.3809e18 —— 注意分组别写成 1_3809_...(那是 1.3809e19)
    assert _normalize_hf(1_380_900_000_000_000_000) == pytest.approx(1.3809)


def test_normalize_hf_uint256_max_is_infinity():
    """无债务时 Aave 返回 uint256.max, 应视为无穷大而非天文数字"""
    assert _normalize_hf(2 ** 256 - 1) == float("inf")


# ── compute_max_borrow ─────────────────────────────────────────

def test_max_borrow_basic_math():
    """抵押 200 / 负债 119.48 / 阈值 82.5% / 目标 HF 1.30

    max_debt = 200 * 0.825 / 1.30 = 126.9231
    headroom = (126.9231 - 119.48) * 0.90 = 6.699
    """
    r = compute_max_borrow(
        collateral_usd=200.0,
        debt_usd=119.48,
        liquidation_threshold=0.825,
        available_usd=40.52,
        hf_target=1.30,
        safety_factor=0.90,
    )
    assert r["max_debt"] == pytest.approx(126.9231, abs=1e-3)
    assert r["headroom"] == pytest.approx(6.699, abs=1e-2)
    assert r["borrow"] == pytest.approx(6.699, abs=1e-2)
    assert r["projected_hf"] >= 1.30


def test_max_borrow_never_breaches_hf_target():
    """核心不变量: 借完之后 HF 必须仍 >= 目标值"""
    r = compute_max_borrow(200.0, 119.48, 0.825, 40.52, 1.30, 0.90)
    assert r["projected_hf"] >= 1.30

    # 换一组参数再验一次
    r2 = compute_max_borrow(1000.0, 100.0, 0.80, 500.0, 1.50, 0.95)
    assert r2["projected_hf"] >= 1.50


def test_max_borrow_is_capped_by_chain_availability():
    """链上可借额度是硬顶: 理论额度再大也借不出来"""
    r = compute_max_borrow(
        collateral_usd=1_000_000.0,
        debt_usd=0.0,
        liquidation_threshold=0.825,
        available_usd=5.0,          # 链上只剩 5 USD
        hf_target=1.30,
        safety_factor=0.90,
    )
    assert r["borrow"] == 5.0


def test_max_borrow_zero_when_no_headroom():
    """HF 已经贴着安全线时, 剩余额度为 0 且不为负"""
    # 债务已经等于 max_debt
    r = compute_max_borrow(200.0, 126.9231, 0.825, 40.52, 1.30, 0.90)
    assert r["headroom"] == pytest.approx(0.0, abs=1e-2)
    assert r["borrow"] == 0.0


def test_max_borrow_negative_headroom_clamps_to_zero():
    """负债已超过安全线对应的债务上限 -> 绝不能算出正的借款额"""
    r = compute_max_borrow(200.0, 200.0, 0.825, 40.52, 1.30, 0.90)
    assert r["borrow"] == 0.0
    assert r["headroom"] <= 0


def test_max_borrow_zero_collateral():
    r = compute_max_borrow(0.0, 10.0, 0.825, 40.52, 1.30, 0.90)
    assert r["borrow"] == 0.0


def test_max_borrow_zero_threshold():
    """清算阈值为 0 (未激活的 reserve) 时不应产生借款"""
    r = compute_max_borrow(200.0, 10.0, 0.0, 40.52, 1.30, 0.90)
    assert r["borrow"] == 0.0


def test_max_borrow_no_debt_still_respects_hf_target():
    """零债务时虽然空间最大, 但借完之后仍必须守在安全线上"""
    r = compute_max_borrow(200.0, 0.0, 0.825, 40.52, 1.30, 0.90)
    assert r["projected_hf"] >= 1.30

    # 只有「完全没有仓位」(抵押为 0 且无债务) 时才是无穷大
    r2 = compute_max_borrow(0.0, 0.0, 0.825, 0.0, 1.30, 0.90)
    assert r2["projected_hf"] == float("inf")


# ── Agent 决策 ─────────────────────────────────────────────────

def _run_agent(client, **cfg_overrides):
    cfg = CapitalEfficiencyConfig(dry_run=True, network="sepolia", **cfg_overrides)
    agent = CapitalEfficiencyAgent(cfg, client=client)
    agent._current_data = agent.fetch_market_data()
    return agent, agent.run_cycle()


def test_agent_borrows_when_position_over_collateralised():
    """HF 1.38 + 40 USD 闲置额度 -> 应产出 borrow action"""
    client = FakeClient(_account(200.0, 119.48, 40.52))
    agent, result = _run_agent(client)

    assert len(result["actions"]) == 1
    a = result["actions"][0]
    assert a["type"] == "REBALANCE"
    assert a["venue"] == "aave-v3"
    assert a["sub_action"] == "borrow"
    assert a["asset"] == "USDC"
    assert a["amount_usd"] == pytest.approx(6.69, abs=0.01)
    assert a["amount_base"] == "6690000"      # 6.69 * 10^6 (USDC 6 位)
    assert a["hf_after"] >= a["hf_target"]
    assert "idle" in a["rationale"]


def test_agent_holds_when_hf_below_idle_threshold():
    """HF 落在 (target, idle) 之间: 安全垫不够厚, 不值得动用额度 -> hold

    200 * 0.825 / 124 = 1.3306, 位于 1.30 与 1.35 之间
    """
    client = FakeClient(_account(200.0, 124.0, 44.0))
    agent, result = _run_agent(client)
    assert result["actions"] == []
    assert "below idle threshold" in result["notes"]


def test_agent_defers_when_hf_at_or_below_target():
    """HF 已 <= target: 这属于 HealthFactorAgent 的防守场景, 本 agent 必须让路"""
    # 165 / 124 = 1.3306 —— 高于 target 但低于 idle, hold
    client = FakeClient(_account(200.0, 124.0, 39.0))
    agent, result = _run_agent(client)
    assert result["actions"] == []
    assert "below idle threshold" in result["notes"]

    # 真正跌破 target 的情形: 165 / 140 = 1.1786
    client2 = FakeClient(_account(200.0, 140.0, 25.0))
    agent2, result2 = _run_agent(client2)
    assert result2["actions"] == []
    assert "deferred to HealthFactorAgent" in result2["notes"]


def test_agent_skips_when_amount_below_minimum():
    """算出来的额度太小时不应发交易 (cover gas)

    先让 HF 高到能触发 (165/119.48 = 1.3809 > 1.35), 再把门槛抬到 50 USD,
    这样 6.69 USD 的额度就不足以构成一次交易。
    """
    client = FakeClient(_account(200.0, 119.48, 40.52))
    agent, result = _run_agent(client, min_borrow_usd=50.0)
    assert result["actions"] == []
    assert "below min" in result["notes"]


def test_agent_degrades_gracefully_on_mcp_error():
    """KeeperHub 挂了不应打挂主循环, 而应返回 read_ok=False"""
    client = FakeClient(error=MCPError("boom"))
    agent, result = _run_agent(client)
    assert result["actions"] == []
    assert result["metrics"]["read_ok"] is False
    assert "boom" in result["notes"]


def test_agent_handles_malformed_account_data():
    client = FakeClient({"totalCollateralBase": "not-a-number"})
    agent, result = _run_agent(client)
    assert result["actions"] == []
    assert result["metrics"]["read_ok"] is False
    assert "malformed" in result["notes"]


def test_agent_no_client_means_no_data():
    """没注入 client 且无 API Key 时, 应优雅降级而不是抛异常"""
    cfg = CapitalEfficiencyConfig(dry_run=True, network="sepolia")
    agent = CapitalEfficiencyAgent(cfg, client=None)
    data = agent.fetch_market_data()
    assert data["available"] is False
    agent._current_data = data
    result = agent.run_cycle()
    assert result["actions"] == []


def test_agent_amount_is_floored_to_cent():
    """金额需向下取整到分, 避免浮点误差导致链上金额与预期不符"""
    client = FakeClient(_account(200.0, 119.48, 40.52))
    agent, result = _run_agent(client)
    a = result["actions"][0]
    # 6.699... 应被截成 6.69, 且 base units 与之严格对应
    assert a["amount_usd"] == round(a["amount_usd"], 2)
    assert int(a["amount_base"]) == int(round(a["amount_usd"] * 1e6))


def test_agent_metrics_are_serialisable():
    """metrics 会进 ERC-8004 快照, 必须能被 json 序列化 (含 inf 处理)"""
    import json

    client = FakeClient(_account(200.0, 119.48, 40.52))
    agent, result = _run_agent(client)
    json.dumps(result["metrics"])   # 不应抛异常


def test_agent_passes_monitor_address_through():
    client = FakeClient(_account(200.0, 119.48, 40.52))
    cfg = CapitalEfficiencyConfig(
        dry_run=True, network="sepolia", monitor_address="0xABCDEF0000000000000000000000000000000001"
    )
    agent = CapitalEfficiencyAgent(cfg, client=client)
    agent.fetch_market_data()
    assert client.calls[0][0] == "0xABCDEF0000000000000000000000000000000001"
