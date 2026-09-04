"""决策层安全不变量 —— 每一条都对应一个真实存在过的缺陷。

执行层那批用例(test_executor_safety.py)守护的是"不盲信上游", 这一批守护
的是"上游自己别算错": 四个决策 agent 的风控模型边界。

发现过程见 scripts/audit_agents.py —— 该脚本以非 pytest 方式复现同一批
结论, 共 19 项探针。这里把其中 13 项已修复的固化成回归测试。

几条贯穿全局的教训:
  * 因子/惩罚项被前置过滤或在取值域上被夹死后, 会静默失效(恒为 1.0、
    被下限钉住)—— 评分模型看起来是三因子, 实际只有两个在起作用。
  * 对齐方向选错(上界向下取整)会静默缩窄区间, 极窄配置下产生空区间。
  * 配置项定义了却从未被引用, 等于该约束不存在。
"""

import math
from decimal import Decimal

import pytest

from capital_efficiency_agent import (
    CapitalEfficiencyAgent,
    CapitalEfficiencyConfig,
    compute_max_borrow,
    round_down_to_cent,
    to_base_units,
)
from grid_agent import GridConfig, GridTradingAgent, clamp_skew, level_sizes
from rebalancing_agent import (
    RebalancingConfig,
    align_down,
    align_up,
)
from yield_agent import (
    YieldConfig,
    YieldOptimisationAgent,
    liquidity_factor,
    stability_factor,
)


# ---------------------------------------------------------------------------
# 构造辅助: 两个 agent 的 __init__ 会连 RPC, 审计与测试都不需要网络。
# 被测逻辑是纯算术, 只依赖 config。
# ---------------------------------------------------------------------------


def make_grid(**kw) -> GridTradingAgent:
    a = GridTradingAgent.__new__(GridTradingAgent)
    a.config = GridConfig(**kw)
    a.pool_address = kw.get("pool_address", "0x" + "11" * 20)
    return a


def make_rebalancing(**kw):
    from rebalancing_agent import RebalancingAgent

    a = RebalancingAgent.__new__(RebalancingAgent)
    a.config = RebalancingConfig(**kw)
    return a


# ---------------------------------------------------------------------------
# GridTradingAgent
# ---------------------------------------------------------------------------


class TestGridSkew:
    """库存偏斜不得把双边报价推过公允价。"""

    def test_clamp_keeps_ask_above_fair(self):
        """修复前: skew 无上界, inv_ratio=1.0 时 ask 被压到 fair 下方 7.36%,
        等于主动贱卖 —— 而 half_spread 只有 7.5bps, 偏移是价差的 100 倍。"""
        a = make_grid(current_inventory=10_000.0, current_cash=0.0)
        a._current_data = {"dex_price": 600.0, "cex_price": 600.0}
        out = a.run_cycle()
        orders = out["actions"][0]["orders"]
        asks = [o["price"] for o in orders if o["side"] == "SELL"]
        assert asks, "应当仍有卖单"
        assert min(asks) > 600.0, f"最低 ask {min(asks)} 跌破了公允价 600"

    def test_clamp_keeps_bid_below_fair(self):
        """对称的情形: 库存为空时 bid 不得涨过公允价(否则是高价接盘)。"""
        a = make_grid(current_inventory=0.0, current_cash=10_000.0)
        a._current_data = {"dex_price": 600.0, "cex_price": 600.0}
        out = a.run_cycle()
        orders = out["actions"][0]["orders"]
        bids = [o["price"] for o in orders if o["side"] == "BUY"]
        assert bids, "应当仍有买单"
        assert max(bids) < 600.0, f"最高 bid {max(bids)} 涨过了公允价 600"

    def test_clamp_skew_respects_half_spread(self):
        """夹紧后 |skew/2| 必须严格小于 half_spread, 这是双边不交叉的充要条件。"""
        for half_spread in (0.000375, 0.001, 0.01, 0.025):
            for raw in (-99.0, -1.0, -0.5, 0.0, 0.5, 1.0, 99.0):
                clamped = clamp_skew(raw, half_spread)
                assert abs(clamped / 2) < half_spread, (
                    f"half_spread={half_spread} raw={raw} -> clamped={clamped} 会交叉"
                )

    def test_clamp_is_noop_when_already_safe(self):
        """夹紧不应改变本就安全的偏斜 —— 否则会削弱均值回归的效果。"""
        assert clamp_skew(0.0001, 0.01) == pytest.approx(0.0001)

    def test_clamping_is_reported_not_silent(self):
        """发生夹紧必须上报。静默夹紧会掩盖"库存失衡已超出价差能吸收的范围"。"""
        a = make_grid(current_inventory=10_000.0, current_cash=0.0)
        a._current_data = {"dex_price": 600.0, "cex_price": 600.0}
        out = a.run_cycle()
        assert out["metrics"]["skew_clamped"] is True


class TestGridInventoryCap:
    """max_inventory 曾经是个死参数 —— 定义了但从未被引用, 等于库存无上限。"""

    def test_max_inventory_is_wired(self):
        a = make_grid(
            current_inventory=20_000.0,
            current_cash=1_000.0,
            max_inventory=10_000.0,
        )
        a._current_data = {"dex_price": 600.0, "cex_price": 600.0}
        out = a.run_cycle()
        orders = out["actions"][0]["orders"]
        sides = {o["side"] for o in orders}
        assert sides == {"SELL"}, f"库存超上限时应只挂卖单去库存, 实际 {sides}"
        assert out["metrics"]["over_inventory"] is True

    def test_normal_inventory_quotes_both_sides(self):
        a = make_grid(current_inventory=5_000.0, current_cash=5_000.0)
        a._current_data = {"dex_price": 600.0, "cex_price": 600.0}
        out = a.run_cycle()
        sides = {o["side"] for o in out["actions"][0]["orders"]}
        assert sides == {"BUY", "SELL"}


class TestGridOrderSizing:
    def test_per_side_total_respects_max_order_size(self):
        """修复前: size = max_order_size / lvl, 三档合计 1.83 倍上限。
        配置名叫 max_order_size 却约束不住, 敞口比配置值大近一倍。"""
        for levels in (1, 2, 3, 5, 10):
            sizes = level_sizes(500.0, levels)
            assert sum(sizes) == pytest.approx(500.0), (
                f"{levels} 档合计 {sum(sizes)} != max_order_size 500"
            )

    def test_level_sizes_decrease_with_distance(self):
        sizes = level_sizes(500.0, 3)
        assert sizes == sorted(sizes, reverse=True), "越远的档位应当越小"

    def test_level_sizes_edge_cases(self):
        assert level_sizes(0.0, 3) == []
        assert level_sizes(500.0, 0) == []

    def test_orders_actually_respect_cap(self):
        a = make_grid(max_order_size=500.0, grid_levels=3)
        a._current_data = {"dex_price": 600.0, "cex_price": 600.0}
        out = a.run_cycle()
        orders = out["actions"][0]["orders"]
        for side in ("BUY", "SELL"):
            total = sum(o["size"] for o in orders if o["side"] == side)
            assert total <= 500.0 + 1e-6, f"{side} 合计 {total} 超出上限"


class TestGridPricePrecision:
    def test_zero_price_orders_are_not_emitted(self):
        """小币(如 fair=4e-7)会被 round(x, 6) 压成 0.0, 而报价 0 意味着
        「任何价格都接受」—— 挂上去就是白送。"""
        a = make_grid()
        a._current_data = {"dex_price": 4e-7, "cex_price": 4e-7}
        out = a.run_cycle()
        orders = out["actions"][0]["orders"]
        assert all(o["price"] > 0 for o in orders), "出现了价格为 0 的订单"
        assert out["metrics"]["skipped_zero_price"] > 0


# ---------------------------------------------------------------------------
# YieldOptimisationAgent
# ---------------------------------------------------------------------------


class TestYieldLiquidityFactor:
    """流动性因子曾恒为 1.0: filter_pools 已把 tvl < min_tvl 的全过滤掉,
    剩下的必然有 tvl/min_tvl >= 1, 于是 min(1.0, ...) 恒等于 1.0 ——
    号称三因子, 实际只有两个在起作用。"""

    def test_distinguishes_widely_different_tvl(self):
        lo = liquidity_factor(1_000_000.0, 1_000_000.0)
        hi = liquidity_factor(500_000_000.0, 1_000_000.0)
        assert hi > lo, f"TVL 相差 500 倍却是同一个因子: {lo} vs {hi}"

    def test_saturates_at_comfort_level(self):
        assert liquidity_factor(100_000_000.0, 1_000_000.0, 100.0) == pytest.approx(1.0)

    def test_never_exceeds_one(self):
        assert liquidity_factor(1e15, 1_000_000.0, 100.0) == pytest.approx(1.0)

    def test_bounded_to_unit_interval(self):
        for tvl in (0.0, -5.0, 1.0, 1e6, 1e9, 1e15):
            f = liquidity_factor(tvl, 1_000_000.0, 100.0)
            assert 0.0 <= f <= 1.0, f"tvl={tvl} -> {f} 越界"

    def test_scoring_uses_it(self):
        a = YieldOptimisationAgent(YieldConfig(min_tvl_usd=1_000_000.0))
        pools = [
            {"apy": 10.0, "apyMean30d": 10.0, "tvlUsd": 1_000_000.0},
            {"apy": 10.0, "apyMean30d": 10.0, "tvlUsd": 500_000_000.0},
        ]
        liqs = [a.risk_adjusted_score(p)[1]["liquidity"] for p in a.filter_pools(pools)]
        assert len(set(liqs)) == 2, f"流动性因子仍失效: {liqs}"


class TestYieldStabilityFactor:
    """稳定性惩罚曾被 max(0.3, 1-deviation) 的下限钉死: 偏离 19 倍的刷量池
    仍拿 0.3 分, 于是 APY 200% 的池得 60 分, 击败 APY 15% 的稳定池(15 分)。"""

    def test_spam_pool_loses_to_stable_pool(self):
        a = YieldOptimisationAgent(YieldConfig())
        scam = {"apy": 200.0, "apyMean30d": 10.0, "tvlUsd": 5_000_000.0}
        solid = {"apy": 15.0, "apyMean30d": 15.0, "tvlUsd": 5_000_000.0}
        assert a.risk_adjusted_score(scam)[0] < a.risk_adjusted_score(solid)[0]

    def test_decays_monotonically(self):
        prev = 1.0
        for deviation in (0.1, 0.5, 1.0, 3.0, 10.0, 19.0):
            f = stability_factor(10.0 * (1 + deviation), 10.0)
            assert f < prev, f"deviation={deviation} 未继续衰减"
            assert f > 0.0, "衰减到 0 会丢失排序信息"
            prev = f

    def test_perfect_stability_scores_one(self):
        assert stability_factor(15.0, 15.0) == pytest.approx(1.0)

    def test_no_arbitrary_floor(self):
        """旧实现的 0.3 下限正是漏洞所在, 极端偏离应当趋近于 0。"""
        assert stability_factor(200.0, 10.0) < 1e-6

    def test_zero_or_negative_mean_is_safe(self):
        assert stability_factor(10.0, 0.0) == 1.0
        assert stability_factor(10.0, -1.0) == 1.0


class TestYieldHoldingAtRisk:
    """持仓池未通过过滤时, 不得谎报"已在最优池"。"""

    def test_ineligible_holding_is_reported_as_risk(self):
        a = YieldOptimisationAgent(YieldConfig(current_pool_id="risky-pool"))
        a._current_data = {
            "pools": [
                {
                    "pool": "good-pool",
                    "chain": "BSC",
                    "apy": 20.0,
                    "apyMean30d": 20.0,
                    "tvlUsd": 5_000_000.0,
                },
                {
                    "pool": "risky-pool",
                    "chain": "BSC",
                    "apy": 5.0,
                    "apyMean30d": 40.0,
                    "tvlUsd": 100_000.0,
                },
            ]
        }
        out = a.run_cycle()
        assert "already in best pool" not in out["notes"], (
            "持仓已不合格却报「已在最优池」—— 把风险粉饰成安全"
        )
        assert "RISK" in out["notes"].upper()

    def test_no_position_still_recommends_entering(self):
        a = YieldOptimisationAgent(YieldConfig(current_pool_id=""))
        a._current_data = {
            "pools": [
                {
                    "pool": "good-pool",
                    "chain": "BSC",
                    "apy": 20.0,
                    "apyMean30d": 20.0,
                    "tvlUsd": 5_000_000.0,
                }
            ]
        }
        out = a.run_cycle()
        assert [act["type"] for act in out["actions"]] == ["ENTER"]

    def test_negative_apy_filtered(self):
        a = YieldOptimisationAgent(YieldConfig())
        pools = [{"apy": -5.0, "apyMean30d": -5.0, "tvlUsd": 5_000_000.0}]
        assert a.filter_pools(pools) == []


# ---------------------------------------------------------------------------
# RebalancingAgent — tick 对齐
# ---------------------------------------------------------------------------


class TestTickAlignment:
    """上界必须向上对齐。修复前上下界都用向下取整, 上界因此偏低; 在极窄
    配置下上下界会撞到同一个 tick, 产生空区间 —— 链上必然 revert。"""

    def test_align_up_rounds_toward_positive_infinity(self):
        assert align_up(47007, 10) == 47010
        assert align_up(47000, 10) == 47000  # 已对齐则不变
        assert align_up(-47007, 10) == -47000  # 负数方向同样朝 +inf

    def test_align_down_rounds_toward_negative_infinity(self):
        assert align_down(47007, 10) == 47000
        assert align_down(-47007, 10) == -47010

    def test_both_aligned_values_are_multiples_of_spacing(self):
        for tick in (-13, -1, 0, 1, 7, 47007):
            for spacing in (1, 10, 60, 200):
                assert align_down(tick, spacing) % spacing == 0
                assert align_up(tick, spacing) % spacing == 0

    def test_zero_spacing_is_identity(self):
        assert align_up(123, 0) == 123
        assert align_down(123, 0) == 123

    def test_range_fully_covers_target(self):
        """对齐后的区间必须完整覆盖配置的目标宽度(只能外扩, 不能内缩)。"""
        price, width_pct, spacing = 100.0, 0.10, 10
        agent = make_rebalancing(range_width_pct=width_pct, dry_run=True)
        r = agent.propose_new_range({"price": price, "tick_spacing": spacing})
        w = width_pct / 100
        assert r["new_price_lower"] <= price * (1 - w), "下界内缩了"
        assert r["new_price_upper"] >= price * (1 + w), "上界内缩了"

    @pytest.mark.parametrize("width_pct", [0.001, 0.005, 0.01, 0.05, 0.1])
    def test_narrow_config_never_yields_empty_range(self, width_pct):
        for spacing in (1, 10, 60, 200):
            agent = make_rebalancing(range_width_pct=width_pct, dry_run=True)
            r = agent.propose_new_range({"price": 100.0, "tick_spacing": spacing})
            assert r["new_tick_upper"] > r["new_tick_lower"], (
                f"width={width_pct}% spacing={spacing} -> "
                f"空区间 [{r['new_tick_lower']}, {r['new_tick_upper']}]"
            )

    def test_collapsed_range_is_widened_and_flagged(self):
        """兜底路径: 当 raw tick 恰好是 spacing 的整数倍时, 向下对齐与向上
        对齐会得到同一个值 —— 此时只剩强制撑开这一道防线。

        构造方式: 让现价略高于某个对齐 tick 对应的价格, 使上下界截断后都
        落在同一个 spacing 倍数上。这是一条极窄但真实可达的路径。
        """
        from rebalancing_agent import tick_to_price

        spacing = 10
        exact_tick = 46050  # 46050 % 10 == 0
        price = tick_to_price(exact_tick) * 1.0000005
        agent = make_rebalancing(range_width_pct=1e-5, dry_run=True)
        r = agent.propose_new_range({"price": price, "tick_spacing": spacing})

        assert r["new_tick_upper"] > r["new_tick_lower"], "兜底未生效, 区间为空"
        assert r["widened_to_min_width"] is True, "撑开了却没标记, 调用方无从得知"
        assert r["new_tick_upper"] - r["new_tick_lower"] == spacing

    def test_normal_width_is_not_flagged(self):
        """常规宽度不该触发撑开标记 —— 否则标记就失去意义了。"""
        agent = make_rebalancing(range_width_pct=10.0, dry_run=True)
        r = agent.propose_new_range({"price": 100.0, "tick_spacing": 10})
        assert r["widened_to_min_width"] is False


# ---------------------------------------------------------------------------
# CapitalEfficiencyAgent — 唯一真上链的决策 agent
# ---------------------------------------------------------------------------


class TestCapitalEfficiencyConfigGuards:
    """风控参数必须在构造时就校验。一个配错的 hf_target 不会在运行时自己
    变好 —— 让它在启动时炸掉, 远比让它在半夜借一笔钱要好。"""

    def test_hf_target_below_floor_rejected(self):
        with pytest.raises(ValueError, match="hf_target"):
            CapitalEfficiencyConfig(hf_target=1.01, hf_idle_threshold=1.05)

    def test_zero_hf_target_rejected_not_division_error(self):
        """修复前会走到 max_debt = ... / hf_target 抛 ZeroDivisionError。"""
        with pytest.raises(ValueError, match="hf_target"):
            CapitalEfficiencyConfig(hf_target=0.0, hf_idle_threshold=1.35)

    def test_contradictory_thresholds_rejected(self):
        """idle_threshold <= target 时两个条件互斥, agent 永远不会借款。"""
        with pytest.raises(ValueError, match="hf_idle_threshold"):
            CapitalEfficiencyConfig(hf_target=1.35, hf_idle_threshold=1.30)

    def test_safety_factor_above_one_rejected(self):
        with pytest.raises(ValueError, match="safety_factor"):
            CapitalEfficiencyConfig(safety_factor=1.5)

    def test_valid_defaults_accepted(self):
        cfg = CapitalEfficiencyConfig()
        assert cfg.hf_target == 1.30


class TestCapitalEfficiencyPrecision:
    def test_round_down_to_cent_is_exact(self):
        """int(1.13 * 100) == 112, 因为 1.13*100 在二进制浮点里是
        112.99999999999999。取整后与「建议金额」对不上, 而对账层正是靠
        比对这两者工作的。"""
        assert round_down_to_cent(1.13) == Decimal("1.13")
        assert round_down_to_cent(2.01) == Decimal("2.01")
        assert round_down_to_cent(8.03) == Decimal("8.03")

    def test_round_down_never_rounds_up(self):
        assert round_down_to_cent(1.9999) == Decimal("1.99")

    def test_to_base_units_is_exact(self):
        assert to_base_units(Decimal("13.23"), 6) == "13230000"
        assert to_base_units(Decimal("6.69"), 6) == "6690000"
        assert to_base_units(Decimal("2.01"), 6) == "2010000"

    def test_full_sweep_matches_decimal(self):
        """全量扫描: 0.01~2000.00 USD 每一档都不得与精确值有偏差。"""
        for cents in range(1, 200_001):
            usd = cents / 100
            assert to_base_units(round_down_to_cent(usd), 6) == str(
                int(Decimal(str(usd)) * 10**6)
            ), f"{usd} 换算不一致"


class TestCapitalEfficiencyBorrow:
    def _agent(self, **kw):
        a = CapitalEfficiencyAgent.__new__(CapitalEfficiencyAgent)
        a.config = CapitalEfficiencyConfig(**kw)
        return a

    def test_tiny_headroom_does_not_broadcast_zero(self):
        """min_borrow_usd 配得比分位小时, 取整会把金额压成 0, 发出一笔借 0
        的交易 —— 白付 gas, 且对账器会判金额不符。"""
        a = self._agent(min_borrow_usd=0.005, hf_target=1.30, hf_idle_threshold=1.35)
        a._current_data = {
            "available": True,
            "collateral_usd": 200.0,
            "debt_usd": 164.99,
            "available_borrow_usd": 0.0099,
            "liquidation_threshold": 0.825,
            "health_factor": 1.999,
        }
        out = a.run_cycle()
        assert out["actions"] == [], "借 0 的交易不应被发出"

    def test_does_not_borrow_when_hf_at_target(self):
        a = self._agent()
        a._current_data = {
            "available": True,
            "collateral_usd": 200.0,
            "debt_usd": 126.0,
            "available_borrow_usd": 50.0,
            "liquidation_threshold": 0.825,
            "health_factor": 1.25,  # < hf_target
        }
        out = a.run_cycle()
        assert out["actions"] == []
        assert "HealthFactorAgent" in out["notes"]

    def test_borrow_keeps_hf_above_target(self):
        a = self._agent()
        a._current_data = {
            "available": True,
            "collateral_usd": 200.0,
            "debt_usd": 100.0,
            "available_borrow_usd": 40.0,
            "liquidation_threshold": 0.825,
            "health_factor": 1.65,
        }
        out = a.run_cycle()
        acts = out["actions"]
        assert acts, "有闲置额度时应当建议借款"
        assert acts[0]["hf_after"] >= a.config.hf_target

    def test_amount_usd_and_base_agree(self):
        a = self._agent()
        a._current_data = {
            "available": True,
            "collateral_usd": 200.0,
            "debt_usd": 100.0,
            "available_borrow_usd": 40.0,
            "liquidation_threshold": 0.825,
            "health_factor": 1.65,
        }
        act = a.run_cycle()["actions"][0]
        assert act["amount_base"] == str(
            int(Decimal(str(act["amount_usd"])) * 10**6)
        )


class TestComputeMaxBorrow:
    def test_no_collateral_means_no_borrow(self):
        r = compute_max_borrow(0.0, 0.0, 0.825, 100.0, 1.30, 0.90)
        assert r["borrow"] == 0.0

    def test_available_is_a_hard_cap(self):
        r = compute_max_borrow(1e9, 0.0, 0.825, 25.0, 1.30, 0.90)
        assert r["borrow"] == pytest.approx(25.0), "链上可借额度应当是硬顶"

    def test_never_negative(self):
        """已超额借用时 headroom 为负, 必须夹到 0 而不是发出负数金额。"""
        r = compute_max_borrow(100.0, 500.0, 0.825, 50.0, 1.30, 0.90)
        assert r["borrow"] == 0.0

    def test_projected_hf_respects_target(self):
        r = compute_max_borrow(200.0, 100.0, 0.825, 40.0, 1.30, 0.90)
        assert r["projected_hf"] >= 1.30

    def test_debt_free_position_is_infinite_hf(self):
        r = compute_max_borrow(200.0, 0.0, 0.825, 0.0, 1.30, 0.90)
        assert math.isinf(r["projected_hf"])
