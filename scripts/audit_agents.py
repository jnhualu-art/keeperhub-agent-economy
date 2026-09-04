"""
业务 agent 深度审计 — 实证脚本
================================

与 scripts/audit_probe.py 同样的原则: **不靠推理下结论, 每个怀疑都跑出证据**。

上一轮审计的是 executor (敌意上游威胁模型)。这一轮审的是决策层 ——
三个业务 agent 的风控模型边界条件。区别在于:

  executor 的漏洞 = 会被攻击
  agent 的漏洞    = 会自己算错, 然后把错误的决策交给已经信任它的 executor

后者更隐蔽, 因为 executor 现在假设上游是可信的(只在金额/上限上兜底),
如果 agent 的风控模型本身算错, executor 的兜底可能根本不触发。

用法:
    python scripts/audit_agents.py

输出每一项: [命中] 表示存在缺陷, [安全] 表示怀疑被证伪。
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))


def _probe_web3() -> bool:
    """记录 web3 是否可用 —— 仅影响报告措辞, 不再注入 stub。

    原先这里会在 web3 装不全时往 sys.modules 塞一个 stub。审计做到一半发现
    stub 是在迁就一个真实缺陷: 两个 agent 把 `from web3 import Web3` 写在
    模块顶层, 而它只被用于 to_checksum_address(RPC 路径)。结果是 web3 依赖
    链一旦损坏, 连纯算术逻辑都 import 不了、测不了。

    现已改为惰性导入(_web3()), 纯算术路径不再依赖 web3。这里只探测状态。
    """
    try:
        import web3  # noqa: F401

        return True
    except Exception:
        return False


USING_REAL_WEB3 = _probe_web3()

results: list[tuple[bool, str, str]] = []
ENV_ISSUES: list[str] = []


def probe(name: str, fn):
    try:
        hit, evidence = fn()
    except (ImportError, ModuleNotFoundError) as exc:
        # 依赖缺失 != 代码缺陷。单列出来, 否则会污染"命中"计数。
        ENV_ISSUES.append(f"{name}: {exc}")
        print(f"[环境] {name}")
        print(f"       依赖缺失, 无法验证: {exc}")
        results.append((False, name, f"依赖缺失: {exc}"))
        return
    except Exception as exc:
        # 其它异常仍然算命中: 说明代码路径不健壮
        hit, evidence = True, f"探针抛出异常 {type(exc).__name__}: {exc}"
    tag = "[命中]" if hit else "[安全]"
    results.append((hit, name, evidence))
    print(f"{tag} {name}")
    print(f"       {evidence}")


def _fmt(v, nd=4):
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


# ===========================================================================
# CapitalEfficiencyAgent —— 唯一会真上链的决策 agent, 优先级最高
# ===========================================================================


def ce_zero_debt():
    """无债务空仓位: available 硬顶会不会把 HF 压到目标以下?

    一度怀疑这是个洞: 无债务时 available = collateral * LTV, 而 LTV < threshold,
    看起来是"链上能力约束"比"安全约束"宽松。实际推导后认为 min() 取的是更小值
    所以永远更安全 —— 用实证确认, 避免把正确代码误报成漏洞。
    """
    from capital_efficiency_agent import compute_max_borrow

    r = compute_max_borrow(
        collateral_usd=200.0,
        debt_usd=0.0,
        liquidation_threshold=0.825,
        available_usd=160.0,  # 200 * LTV(0.80)
        hf_target=1.30,
        safety_factor=0.90,
    )
    phf = r["projected_hf"]
    return (
        phf < 1.30,
        f"无债务: borrow={_fmt(r['borrow'], 2)} USD, projected_hf={_fmt(phf)} "
        f"({'低于' if phf < 1.30 else '高于'} target 1.30)",
    )


def ce_zero_debt_extreme():
    """极端版: available 远大于安全额度(LTV 异常高或预言机异常)"""
    from capital_efficiency_agent import compute_max_borrow

    r = compute_max_borrow(
        collateral_usd=200.0,
        debt_usd=0.0,
        liquidation_threshold=0.825,
        available_usd=10_000_000.0,  # 荒谬的可用额度
        hf_target=1.30,
        safety_factor=0.90,
    )
    phf = r["projected_hf"]
    return (
        phf < 1.30,
        f"available=1000万: borrow={_fmt(r['borrow'], 2)}, projected_hf={_fmt(phf)} "
        f"-> borrow 由 headroom({_fmt(r['headroom'], 2)}) 而非 available 决定, 安全",
    )


def ce_low_hf_target():
    """hf_target 配得离清算线太近, 应当在构造配置时就被拒绝

    修复前: 配 1.01 会被接受, agent 照着"保住 HF 1.01"借出 39.49 USD,
    把 HF 压到 1.0379 —— 离清算线 3.8%, 而 executor 的清算地板是 1.0, 不拦。
    修复后: 构造 CapitalEfficiencyConfig 时抛 ValueError。
    """
    from capital_efficiency_agent import (
        MIN_HF_TARGET,
        CapitalEfficiencyAgent,
        CapitalEfficiencyConfig,
    )

    try:
        agent = CapitalEfficiencyAgent(
            CapitalEfficiencyConfig(hf_target=1.01, hf_idle_threshold=1.05, dry_run=True)
        )
    except ValueError as exc:
        return (False, f"配置在构造时被拒: {str(exc)[:90]}...")

    agent._current_data = {
        "available": True,
        "collateral_usd": 200.0,
        "debt_usd": 119.48,
        "available_borrow_usd": 40.52,
        "liquidation_threshold": 0.825,
        "health_factor": 1.3810,
    }
    out = agent.run_cycle()
    acts = out.get("actions") or []
    if not acts:
        return (False, "未发出 action")
    hf_after = acts[0].get("hf_after")
    return (
        hf_after < MIN_HF_TARGET,
        f"hf_target=1.01 竟被接受, 借出 {_fmt(acts[0]['amount_usd'], 2)} USD, "
        f"hf_after={hf_after} —— 离清算线仅 {(hf_after - 1.0) * 100:.1f}%",
    )


def ce_zero_hf_target():
    """hf_target=0 应当被拒绝, 而不是抛 ZeroDivisionError"""
    from capital_efficiency_agent import CapitalEfficiencyAgent, CapitalEfficiencyConfig

    try:
        agent = CapitalEfficiencyAgent(
            CapitalEfficiencyConfig(hf_target=0.0, hf_idle_threshold=1.35, dry_run=True)
        )
    except ValueError as exc:
        return (False, f"配置在构造时被拒(正确): {str(exc)[:70]}...")

    agent._current_data = {
        "available": True,
        "collateral_usd": 200.0,
        "debt_usd": 100.0,
        "available_borrow_usd": 50.0,
        "liquidation_threshold": 0.825,
        "health_factor": 1.65,
    }
    try:
        agent.run_cycle()
    except ZeroDivisionError:
        return (True, "hf_target=0 抛出 ZeroDivisionError, 未被校验拦截")
    return (False, "未除零")


def ce_rounding_float():
    """round_down_to_cent 是否精确

    修复前用的是 int(borrow * 100) / 100, 二进制浮点下 1.13*100 == 112.9999...
    -> 得到 1.12。取整方向是向下所以不产生资金风险, 但会让建议金额与链上
    实际金额对不上, 而对账层正是靠比对这两者工作的。
    """
    from capital_efficiency_agent import round_down_to_cent

    bad = []
    for cents in range(1, 2_000_001):  # 0.01 ~ 20000.00 USD, 全扫
        b = cents / 100
        if round_down_to_cent(b) != Decimal(str(b)).quantize(Decimal("0.01")):
            bad.append(b)
            if len(bad) >= 5:
                break
    if bad:
        return (
            True,
            f"{len(bad)}+ 个金额取整后与精确值不符, 例: "
            + ", ".join(f"{v:.2f}->{round_down_to_cent(v)}" for v in bad[:5]),
        )
    return (False, "0.01~20000.00 USD 全量扫描, 取整结果与精确值完全一致")


def ce_amount_base_precision():
    """to_base_units 是否精确(USDC 6 位小数)"""
    from capital_efficiency_agent import round_down_to_cent, to_base_units

    bad = []
    for cents in range(1, 200_001):  # 0.01 ~ 2000.00 USD
        b = cents / 100
        got = to_base_units(round_down_to_cent(b), 6)
        want = str(int(Decimal(str(b)) * 10**6))
        if got != want:
            bad.append((b, got, want))
            if len(bad) >= 5:
                break
    if bad:
        return (
            True,
            f"{len(bad)}+ 个 amount_base 与精确值不符, 例: "
            + ", ".join(f"{v:.2f}: {g} != {w}" for v, g, w in bad[:3]),
        )
    return (False, "0.01~2000.00 USD 全量扫描, amount_base 与精确值完全一致")


def ce_rounding_zero():
    """min_borrow_usd 配得比分位小时, 取整会把金额压成 0

    修 run_cycle 里加了取整后 > 0 的复核, 这里验证取整函数本身以及
    run_cycle 的拒绝路径。
    """
    from capital_efficiency_agent import (
        CapitalEfficiencyAgent,
        CapitalEfficiencyConfig,
        round_down_to_cent,
    )

    zero = round_down_to_cent(0.0099)
    # 再验证 run_cycle 会不会真的发出借 0 的 action
    agent = CapitalEfficiencyAgent(
        CapitalEfficiencyConfig(
            min_borrow_usd=0.005,
            hf_target=1.30,
            hf_idle_threshold=1.35,
            safety_factor=0.90,
            dry_run=True,
        )
    )
    # 构造一个 headroom 只有几美分的仓位
    agent._current_data = {
        "available": True,
        "collateral_usd": 10.0,
        "debt_usd": 6.30,
        "available_borrow_usd": 0.0099,
        "liquidation_threshold": 0.825,
        "health_factor": 1.35,
    }
    out = agent.run_cycle()
    acts = out.get("actions") or []
    emitted_zero = bool(acts) and acts[0].get("amount_base") == "0"
    return (
        emitted_zero or zero < 0,
        f"round_down_to_cent(0.0099)={zero}; run_cycle 发出 "
        f"{len(acts)} 个 action, notes={out.get('notes')!r} -> "
        f"{('仍然会借 0!' if emitted_zero else '已被拒绝')}",
    )


# ===========================================================================
# GridTradingAgent
# ===========================================================================


def _grid_agent(**cfg_kw):
    """绕过 __init__ 构造: GridTradingAgent 的构造函数会连 RPC, 审计不需要网络

    只补 run_cycle 真正会读的属性, 其余保持缺失 —— 万一将来 run_cycle
    开始依赖新属性, 审计脚本会立刻炸掉, 而不是悄悄跳过。
    """
    from grid_agent import GridConfig, GridTradingAgent

    a = GridTradingAgent.__new__(GridTradingAgent)
    a.config = GridConfig(**cfg_kw)
    a.pool_address = "0x" + "0" * 40
    return a


def grid_skew_crosses_fair():
    """库存偏斜可以把报价推过公允价 —— 那是确定性亏损, 不是做市

    skew 位移量没有 clamp 到半价差以内。库存极端时 skew/2 可以远大于 half_spread,
    导致 ask 低于公允价(贱卖)或 bid 高于公允价(贵买)。
    """
    a = _grid_agent(current_inventory=10_000.0, current_cash=0.0)  # 全部是货
    a._current_data = {"dex_price": 600.0, "cex_price": 600.0, "atr_pct": None}
    out = a.run_cycle()
    orders = out.get("orders") or []
    if not orders:
        return (False, "未生成订单")
    asks = [o for o in orders if o["side"] == "SELL"]
    bids = [o for o in orders if o["side"] == "BUY"]
    worst_ask = min(o["price"] for o in asks)
    worst_bid = max(o["price"] for o in bids)
    fair = 600.0
    crossed_ask = worst_ask < fair
    crossed_bid = worst_bid > fair
    return (
        crossed_ask or crossed_bid,
        f"fair={fair}: 最低 ask={worst_ask} ({'低于公允价, 贱卖' if crossed_ask else '正常'}), "
        f"最高 bid={worst_bid} ({'高于公允价, 贵买' if crossed_bid else '正常'}); "
        f"偏移 {(abs(worst_ask - fair) / fair * 100):.2f}% / "
        f"{(abs(worst_bid - fair) / fair * 100):.2f}%, 而 half_spread 仅 "
        f"{out['metrics']['spread_bps'] / 2:.1f}bps",
    )


def grid_max_inventory_dead():
    """max_inventory 定义了但从未接线 —— 虚假的风控参数

    和上一轮 executor 里"宣称幂等但键是时间戳"是同一类问题:
    配置里躺着一个看起来在管风险的数字, 实际上没人读它。
    """
    import inspect

    import grid_agent

    src = inspect.getsource(grid_agent.GridTradingAgent)
    # 构造参数里定义了, 但 run_cycle / fetch_market_data 里没引用
    defined_in_config = "max_inventory" in inspect.getsource(grid_agent.GridConfig)
    used = "max_inventory" in src
    return (
        defined_in_config and not used,
        f"GridConfig 定义了 max_inventory={grid_agent.GridConfig().max_inventory}, "
        f"但 GridTradingAgent 全部代码里{'未' if not used else '已'}引用它 -> "
        f"{'死参数, 库存无上限' if not used else '正常'}",
    )


def grid_price_rounds_to_zero():
    """round(price, 6) 会把小币价格压成 0

    BSC 上大量 meme 币单价远小于 1e-6。报价 0 是灾难性的。
    """
    a = _grid_agent()
    a._current_data = {"dex_price": 4e-7, "cex_price": None, "atr_pct": None}
    out = a.run_cycle()
    orders = out.get("orders") or []
    if not orders:
        return (False, "未生成订单")
    prices = [o["price"] for o in orders]
    zero = [p for p in prices if p == 0]
    return (
        bool(zero),
        f"fair=4e-7 -> 订单价格 {prices[:4]}... 其中 {len(zero)}/{len(prices)} 个为 0 "
        f"({('报价 0 会以任何价格成交' if zero else '正常')})",
    )


def grid_total_size_exceeds_cap():
    """各层挂单量之和超过 max_order_size

    size = max_order_size / lvl, 三层合计 = 500 * (1 + 1/2 + 1/3) = 916.7,
    接近配置上限的两倍。
    """
    a = _grid_agent(max_order_size=500.0, grid_levels=3)
    a._current_data = {"dex_price": 600.0, "cex_price": None, "atr_pct": None}
    out = a.run_cycle()
    orders = out.get("orders") or []
    buy_total = sum(o["size"] for o in orders if o["side"] == "BUY")
    return (
        buy_total > 500.0 + 1e-9,
        f"单边三档合计 {buy_total:.2f}, 而 max_order_size=500.00 -> "
        f"{('超出发单上限, 配置名不副实' if buy_total > 500 else '正常')}",
    )


# ===========================================================================
# YieldOptimisationAgent
# ===========================================================================


def yield_liquidity_factor_dead():
    """评分模型宣称三因子, 实际只有两因子在起作用

    filter_pools 已经用 tvl < min_tvl_usd 过滤过, 所以进入评分的池必有
    tvl >= min_tvl_usd -> liquidity = min(1.0, tvl/min_tvl) 恒等于 1.0。
    """
    from yield_agent import YieldConfig, YieldOptimisationAgent

    a = YieldOptimisationAgent(YieldConfig(min_tvl_usd=1_000_000.0))
    pools = [
        {"pool": "a", "chain": "BSC", "apy": 10.0, "apyMean30d": 10.0, "tvlUsd": 1_000_000.0},
        {"pool": "b", "chain": "BSC", "apy": 10.0, "apyMean30d": 10.0, "tvlUsd": 500_000_000.0},
    ]
    filtered = a.filter_pools(pools)
    liqs = [a.risk_adjusted_score(p)[1]["liquidity"] for p in filtered]
    distinct = len(set(liqs))
    return (
        distinct == 1 and liqs and liqs[0] == 1.0,
        f"TVL 相差 500 倍的两个池子, 流动性因子都是 {liqs} -> "
        f"{'该因子已失效(被前置过滤抵消)' if distinct == 1 else '正常'}",
    )


def yield_stability_floor_too_high():
    """稳定性惩罚太弱: 高波动池可以靠下限 0.3 击败稳定池

    apy 偏离 30 日均值 19 倍(典型的新池刷量骗局), stability 仍取 0.3,
    于是 200% APY 的池子得分是 15% 稳定池的 4 倍。
    """
    from yield_agent import YieldConfig, YieldOptimisationAgent

    a = YieldOptimisationAgent(YieldConfig())
    scam = {"apy": 200.0, "apyMean30d": 10.0, "tvlUsd": 5_000_000.0}  # 偏离 19 倍
    solid = {"apy": 15.0, "apyMean30d": 15.0, "tvlUsd": 5_000_000.0}
    s_scam, d_scam = a.risk_adjusted_score(scam)
    s_solid, d_solid = a.risk_adjusted_score(solid)
    return (
        s_scam > s_solid,
        f"刷量池(APY 200%, 30日均值 10%) score={_fmt(s_scam, 2)} "
        f"击败稳定池(APY 15%, 30日均值 15%) score={_fmt(s_solid, 2)}; "
        f"stability 被下限钉在 {d_scam['stability']}",
    )


def yield_missing_position_misreported():
    """当前持仓没通过风控过滤时, 报告说"已经在最优池"

    current_id 非空但 current 为 None(当前池 TVL 跌破下限 / APY 越界)时,
    代码走 else 分支输出 "already in best pool -> hold" —— 事实上是
    "当前持仓已不符合风控标准, 但我们不知道它在哪"。
    """
    from yield_agent import YieldConfig, YieldOptimisationAgent

    a = YieldOptimisationAgent(YieldConfig(current_pool_id="risky-pool"))
    a._current_data = {
        "pools": [
            {"pool": "good-pool", "chain": "BSC", "apy": 20.0, "apyMean30d": 20.0, "tvlUsd": 5_000_000.0},
            # 当前持仓: TVL 只剩 10 万, 低于 min_tvl_usd, 会被过滤掉
            {"pool": "risky-pool", "chain": "BSC", "apy": 5.0, "apyMean30d": 40.0, "tvlUsd": 100_000.0},
        ]
    }
    out = a.run_cycle()
    notes = out.get("notes", "")
    return (
        "already in best" in notes,
        f"当前池 TVL=10万(低于下限, 已被过滤) 时报告: {notes!r} -> "
        f"{'漏报: 持仓风险未提示' if 'already in best' in notes else '正确提示'}",
    )


def yield_negative_apy():
    """负 APY 池是否会被过滤

    DefiLlama 的某些池(尤其是带 impermanent loss 的)会报负 APY。
    min_apy_pct=0.5 应该挡住, 但要看 apy 为 None 时的处理。
    """
    from yield_agent import YieldConfig, YieldOptimisationAgent

    a = YieldOptimisationAgent(YieldConfig())
    pools = [
        {"pool": "neg", "apy": -12.0, "apyMean30d": -12.0, "tvlUsd": 5_000_000.0},
        {"pool": "nan", "apy": None, "apyMean30d": 10.0, "tvlUsd": 5_000_000.0},
    ]
    kept = a.filter_pools(pools)
    return (
        any(p["pool"] == "neg" for p in kept),
        f"负 APY 池被{'保留' if any(p['pool'] == 'neg' for p in kept) else '过滤'}"
        f"(保留 {[p['pool'] for p in kept]})",
    )


# ===========================================================================
# RebalancingAgent
# ===========================================================================


def rebalancing_route_conflict():
    """两个 agent 都发 type=REBALANCE, executor 会怎么分派?

    capital_efficiency 的带 venue=aave-v3 + sub_action=borrow;
    rebalancing 的带 venue=pancake-v3 + token_id(没有 sub_action)。
    如果舰队同时启用两者, 需要确认 executor 不会把 LP 重置动作当成借款。
    """
    import executor as ex

    class SpyClient:
        def __init__(self):
            self.calls = []

        def borrow(self, **kw):
            self.calls.append(("borrow", kw))
            return {"transactionHash": "0xabc"}

    spy = SpyClient()
    e = ex.Executor(dry_run=False, client=spy)
    # rebalancing_agent 产生的 action 形状
    lp_action = {
        "type": "REBALANCE",
        "venue": "pancake-v3",
        "token_id": 2690498,
        "pair": "WBNB/USDT",
        "new_tick_lower": -100,
        "new_tick_upper": 100,
    }
    rec = e.execute_action(lp_action)
    borrowed = [c for c in spy.calls if c[0] == "borrow"]
    return (
        bool(borrowed),
        f"LP 重置 action 进入 executor -> "
        f"{('被当成借款执行! ' + str(borrowed[0][1])) if borrowed else '未触发借款'}, "
        f"note={rec.get('note')!r}",
    )


def _rebalancing_agent(**cfg_kw):
    """绕过 __init__ 构造: 它会连 BSC RPC 建合约对象, 审计不需要网络。

    propose_new_range 是纯算术, 只依赖 self.config。
    """
    from rebalancing_agent import RebalancingAgent, RebalancingConfig

    a = RebalancingAgent.__new__(RebalancingAgent)
    a.config = RebalancingConfig(**cfg_kw)
    return a


def rebalancing_upper_tick_floor():
    """新区间上界应向上对齐到 tickSpacing

    测端到端产出(agent 真正送上链的区间参数), 而不是内部 helper。
    原实现对上下界都用向下取整, 上界因此偏低: 新区间比配置宽度窄,
    更容易再次脱出区间 —— 而这正是本 agent 要解决的问题。

    判据: 对齐后的区间必须完整覆盖 [price×(1-w), price×(1+w)],
    即下界 <= 目标下界 且 上界 >= 目标上界。
    """
    price, width_pct, spacing = 100.0, 0.10, 10
    agent = _rebalancing_agent(range_width_pct=width_pct, dry_run=True)
    r = agent.propose_new_range({"price": price, "tick_spacing": spacing})

    target_lo, target_hi = price * (1 - width_pct / 100), price * (1 + width_pct / 100)
    ok = r["new_price_lower"] <= target_lo and r["new_price_upper"] >= target_hi
    return (
        not ok,
        f"目标区间 [{target_lo:.4f}, {target_hi:.4f}] -> 实际 "
        f"[{r['new_price_lower']:.4f}, {r['new_price_upper']:.4f}] "
        f"(tick {r['new_tick_lower']}~{r['new_tick_upper']}) -> "
        f"{'完整覆盖, 对齐方向正确' if ok else '区间偏窄, 上界被向下对齐'}",
    )


def rebalancing_degenerate_range():
    """极窄配置会算出空/倒置区间

    range_width_pct 小于一个 tickSpacing 对应的价格宽度时, 上下界会被对齐到
    同一个 tick。空区间上链必然 revert, 且失败前从数据上完全看不出来。
    """
    from rebalancing_agent import RebalancingAgent, RebalancingConfig

    for width_pct in (0.001, 0.005, 0.01, 0.05):
        agent = _rebalancing_agent(range_width_pct=width_pct, dry_run=True)
        r = agent.propose_new_range({"price": 100.0, "tick_spacing": 10})
        if r["new_tick_upper"] <= r["new_tick_lower"]:
            return (
                True,
                f"range_width_pct={width_pct} -> tick "
                f"[{r['new_tick_lower']}, {r['new_tick_upper']}] "
                f"区间为空/倒置(链上必然失败)",
            )
    return (False, "测试的几档宽度均产生有效区间")


def rebalancing_negative_price():
    """price_to_tick 对非法价格的防御

    文档说 price<=0 会 raise ValueError。若上游 slot0 返回异常值(比如
    合约出错返回 0), 异常会一路冒到主循环。
    """
    from rebalancing_agent import price_to_tick

    for bad in (0.0, -1.0):
        try:
            price_to_tick(bad)
            return (True, f"price_to_tick({bad}) 未抛异常")
        except ValueError:
            continue
    return (False, "非法价格均被 ValueError 拦截")


# ===========================================================================

print("=" * 78)
print("业务 agent 深度审计 (实证)")
print("=" * 78)
print()

print("── CapitalEfficiencyAgent (唯一真上链的决策 agent) ────────────")
probe("无债务空仓位会把 HF 压破目标吗", ce_zero_debt)
probe("available 远大于安全额度时", ce_zero_debt_extreme)
probe("hf_target 配置过低无护栏", ce_low_hf_target)
probe("hf_target=0 应拒绝而非除零", ce_zero_hf_target)
probe("金额取整到分的浮点截断", ce_rounding_float)
probe("amount_base 的浮点精度", ce_amount_base_precision)
probe("min_borrow 过小导致借 0", ce_rounding_zero)

print()
print("── GridTradingAgent ───────────────────────────────────────────")
probe("库存偏斜可把报价推过公允价", grid_skew_crosses_fair)
probe("max_inventory 是死参数", grid_max_inventory_dead)
probe("小币价格被 round 成 0", grid_price_rounds_to_zero)
probe("各档合计超出 max_order_size", grid_total_size_exceeds_cap)

print()
print("── YieldOptimisationAgent ─────────────────────────────────────")
probe("流动性因子恒为 1.0 (三因子变两因子)", yield_liquidity_factor_dead)
probe("稳定性惩罚太弱, 刷量池可胜出", yield_stability_floor_too_high)
probe("持仓未通过过滤时误报已在最优池", yield_missing_position_misreported)
probe("负 APY 池的过滤", yield_negative_apy)

print()
print("── RebalancingAgent ───────────────────────────────────────────")
probe("REBALANCE 路由冲突(被当借款?)", rebalancing_route_conflict)
probe("上界 tick 向下对齐致区间偏窄", rebalancing_upper_tick_floor)
probe("极窄配置产生空区间", rebalancing_degenerate_range)
probe("非法价格的防御", rebalancing_negative_price)

print()
print("=" * 78)
hits = [r for r in results if r[0]]
print(f"命中 {len(hits)} / {len(results)}")
if ENV_ISSUES:
    print(f"另有 {len(ENV_ISSUES)} 项因依赖缺失无法验证(非代码缺陷):")
    for e in ENV_ISSUES:
        print(f"  - {e}")
if not USING_REAL_WEB3:
    print("注: 本机 web3 依赖链不完整, 但已不影响本审计 —— 两个 agent 已改为")
    print("    惰性导入 web3, 被测的纯算术路径不需要它。仅 RPC 路径不可用。")
print("=" * 78)
