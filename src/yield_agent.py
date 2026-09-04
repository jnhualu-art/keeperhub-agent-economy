"""
Yield Optimisation Agent
========================
官方类别: Yield Optimisation — "Routes liquidity to the highest available APR"

数据源: DefiLlama Yields API (https://yields.llama.fi/pools)
  实测 BSC 链上 636 个池子, 其中 TVL>$1M 且 APY 合理的优质池约 49 个。

决策逻辑(真实, 非 mock):
  1. 拉取 BSC 全部池子的 APY / TVL / 30 日均值
  2. 过滤噪音: TVL 下限 + APY 上下限(剔除 24412% 这类小池骗局)
  3. 风险调整评分 = APY × 稳定性 × 流动性
       - 稳定性: 当前 APY 与 30 日均值偏离越大, 信心越低
       - 流动性: TVL 低于目标线性衰减
  4. 与当前持仓对比, 提升超过阈值(默认 15%, 覆盖 gas 成本)才建议迁移
  5. dry_run 默认开启, 只输出建议不发起交易
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

from base_agent import (
    CATEGORY_YIELD,
    AgentConfig,
    BaseAgent,
)

DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache_defillama_pools.json"
)
CACHE_TTL_SEC = 300  # 全量约 12MB, 5 分钟缓存足够


def stability_factor(apy: float, apy_mean_30d: float, decay: float = 1.0) -> float:
    """APY 相对 30 日均值的可信度, 落在 (0, 1]。

    用 exp(-decay × deviation) 而不是 max(0.3, 1 - deviation): 后者给偏离
    20 倍的刷量池仍留 0.3 分, 于是 APY 200%(30日均值 10%) 的池子能拿
    200 × 0.3 = 60 分, 击败 APY 15% 的稳定池(15 分) —— 惩罚形同虚设。

    指数衰减下同样的例子得分是 200 × e^-19 ≈ 0, 且对偏离单调, 即使所有
    候选都很不稳定, argmax 仍会挑出最不差的那个。
    """
    if apy_mean_30d <= 0:
        return 1.0
    deviation = abs(apy - apy_mean_30d) / apy_mean_30d
    return math.exp(-decay * deviation)


def liquidity_factor(
    tvl: float, min_tvl: float, saturation_mult: float = 100.0
) -> float:
    """TVL 的流动性评分, 落在 [0, 1]。

    原写法 min(1.0, tvl / min_tvl) 恒等于 1.0: 因为 filter_pools 已经把
    tvl < min_tvl 的池子全过滤掉了, 剩下的必然有 tvl/min_tvl >= 1。于是
    号称「APY × 稳定性 × 流动性」的三因子模型实际只剩两个因子在起作用。

    把「资格门槛」(min_tvl, 硬过滤) 和「评分基准」(饱和点) 分开: 刚过线
    的池子得 0 分附近, 达到 min_tvl × saturation_mult 才拿满分。对数而非
    线性, 因为流动性的边际收益递减。
    """
    if tvl <= 0 or min_tvl <= 0:
        return 0.0
    # 饱和点必须严格大于门槛, 否则分母为 0 或负, 因子失去意义
    span = math.log(max(saturation_mult, 1.0001))
    raw = math.log(max(tvl / min_tvl, 1.0)) / span
    return max(0.0, min(1.0, raw))


@dataclass
class YieldConfig(AgentConfig):
    """Yield agent 专属参数"""

    min_tvl_usd: float = 1_000_000.0     # 流动性下限(硬过滤: 低于此不参与)
    min_apy_pct: float = 0.5             # 低于此不值得
    max_apy_pct: float = 200.0           # 高于此视为噪音/风险
    rebalance_threshold: float = 0.15    # 相对提升 15% 才迁移
    top_n: int = 5                       # 输出前 N 个候选
    current_pool_id: str = ""            # 当前持仓池(空=空仓)
    # 流动性因子的饱和点 = min_tvl_usd × 该倍数。必须显著大于 1: 否则刚过
    # 硬过滤线的池子就能拿满分, 因子恒为 1.0 形同虚设(见 liquidity_factor)。
    liquidity_saturation_mult: float = 100.0
    # 稳定性衰减系数: APY 相对 30 日均值的偏离按 exp(-k × deviation) 惩罚。
    stability_decay: float = 1.0


class YieldOptimisationAgent(BaseAgent):
    CATEGORY = CATEGORY_YIELD

    def __init__(self, config: YieldConfig | None = None):
        super().__init__(config or YieldConfig())
        self.config: YieldConfig

    # ------------------------------------------------------------------
    # 数据层
    # ------------------------------------------------------------------

    def fetch_market_data(self) -> dict:
        """拉取 DefiLlama 池子数据(带本地缓存)"""
        pools = self._load_pools()
        return {"timestamp": time.time(), "count": len(pools), "pools": pools}

    def _load_pools(self) -> list[dict]:
        # 缓存命中
        if os.path.exists(CACHE_PATH):
            age = time.time() - os.path.getmtime(CACHE_PATH)
            if age < CACHE_TTL_SEC:
                try:
                    with open(CACHE_PATH, encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        resp = httpx.get(DEFILLAMA_POOLS_URL, timeout=30.0)
        resp.raise_for_status()
        all_pools = resp.json().get("data", [])

        bsc = [
            p for p in all_pools
            if p.get("chain") in ("Binance", "BSC")
        ]

        # 落盘缓存(只存 BSC 部分, 省空间)
        try:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(bsc, f)
        except Exception:
            pass

        return bsc

    # ------------------------------------------------------------------
    # 策略核心
    # ------------------------------------------------------------------

    def filter_pools(self, pools: list[dict]) -> list[dict]:
        """过滤噪音池"""
        cfg = self.config
        out = []
        for p in pools:
            apy = p.get("apy")
            tvl = p.get("tvlUsd")
            if apy is None or tvl is None:
                continue
            if tvl < cfg.min_tvl_usd:
                continue
            if not (cfg.min_apy_pct <= apy <= cfg.max_apy_pct):
                continue
            out.append(p)
        return out

    def risk_adjusted_score(self, pool: dict) -> tuple[float, dict]:
        """
        风险调整后收益评分。

        返回 (score, 明细)
        """
        apy = pool.get("apy") or 0.0
        apy30 = pool.get("apyMean30d") or apy
        tvl = pool.get("tvlUsd") or 0.0

        # 1) 稳定性: 当前 APY 偏离 30 日均值越多, 越不可信
        stability = stability_factor(
            apy, apy30, decay=self.config.stability_decay
        )

        # 2) 流动性: 相对硬过滤下限的对数增长, 到饱和点拿满分
        liquidity = liquidity_factor(
            tvl,
            min_tvl=self.config.min_tvl_usd,
            saturation_mult=self.config.liquidity_saturation_mult,
        )

        score = apy * stability * liquidity
        detail = {
            "apy": round(apy, 2),
            "apy_mean_30d": round(apy30, 2),
            "tvl_usd": round(tvl, 0),
            "stability": round(stability, 3),
            "liquidity": round(liquidity, 3),
            "score": round(score, 3),
        }
        return score, detail

    def run_cycle(self) -> dict:
        pools = self.filter_pools(self._current_data.get("pools", []))

        if not pools:
            return {
                "metrics": {"candidates": 0},
                "actions": [],
                "notes": "no pool passes filters",
            }

        scored = []
        for p in pools:
            score, detail = self.risk_adjusted_score(p)
            scored.append(
                {
                    "pool_id": p.get("pool"),
                    "project": p.get("project"),
                    "symbol": p.get("symbol"),
                    "chain": p.get("chain"),
                    "stablecoin": p.get("stablecoin"),
                    **detail,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[: self.config.top_n]
        best = top[0]

        # ---- 决策: 是否迁移 ----
        actions = []
        notes = ""
        current_id = self.config.current_pool_id
        current = next((s for s in scored if s["pool_id"] == current_id), None)

        # 持仓池没出现在候选里有两种可能, 必须区分:
        #   (a) 空仓 —— 正常, 应当建仓
        #   (b) 有持仓但该池没通过过滤 —— 风险信号, 原实现会走进下面的 else
        #       分支报 "already in best pool -> hold", 把「持仓已经不合格」
        #       粉饰成「持仓就是最优」。这正是监控型 agent 最不该犯的错。
        current_ineligible = bool(current_id) and current is None

        if current_ineligible:
            notes = (
                f"HOLDING AT RISK: current pool {current_id} did not pass "
                f"filters (TVL < {self.config.min_tvl_usd:,.0f} or APY outside "
                f"[{self.config.min_apy_pct}, {self.config.max_apy_pct}]) -> "
                f"manual review needed; not claiming it is the best pool"
            )
        elif not current_id:
            notes = "no current position -> recommend entering best pool"
            actions.append(
                {
                    "type": "ENTER",
                    "pool_id": best["pool_id"],
                    "symbol": best["symbol"],
                    "project": best["project"],
                    "expected_apy": best["apy"],
                    "dry_run": self.config.dry_run,
                }
            )
        elif current and current["pool_id"] != best["pool_id"]:
            uplift = (best["score"] - current["score"]) / current["score"] if current["score"] else 0
            if uplift >= self.config.rebalance_threshold:
                notes = f"uplift {uplift:.1%} >= {self.config.rebalance_threshold:.0%} -> migrate"
                actions.append(
                    {
                        "type": "MIGRATE",
                        "from_pool": current["symbol"],
                        "to_pool_id": best["pool_id"],
                        "to_symbol": best["symbol"],
                        "to_project": best["project"],
                        "uplift_pct": round(uplift * 100, 2),
                        "from_apy": current["apy"],
                        "to_apy": best["apy"],
                        "dry_run": self.config.dry_run,
                    }
                )
            else:
                notes = f"uplift {uplift:.1%} below threshold -> hold"
        else:
            notes = "already in best pool -> hold"

        return {
            "metrics": {
                "candidates": len(scored),
                "best_pool": best["symbol"],
                "best_project": best["project"],
                "best_apy": best["apy"],
                "best_score": best["score"],
                "current_pool": current["symbol"] if current else None,
                "current_apy": current["apy"] if current else None,
            },
            "actions": actions,
            "notes": notes,
            "top": top,
        }

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = YieldConfig(
        agent_name="yieldpilot.agent",
        agent_description=(
            "Routes stablecoin liquidity across BSC pools by risk-adjusted APY. "
            "Filters noise pools via TVL floor and APY sanity bounds, scores "
            "candidates by APY x stability(30d mean deviation) x liquidity(TVL), "
            "and migrates only when uplift exceeds the gas-cost threshold."
        ),
        dry_run=True,
        network="mainnet",
        cycle_interval_sec=0,   # demo 模式连续跑
    )

    agent = YieldOptimisationAgent(cfg)
    history = agent.run(cycles=2)

    print("\n" + "=" * 70)
    print("FINAL STATUS")
    print("=" * 70)
    print(json.dumps(agent.current_status(), ensure_ascii=False, indent=2))

    # 输出 registration file 供 ERC-8004 注册
    reg_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "yieldpilot_registration.json"
    )
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(agent.to_registration_file(), f, ensure_ascii=False, indent=2)
    print(f"\nERC-8004 registration file -> {reg_path}")


if __name__ == "__main__":
    _main()
