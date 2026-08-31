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


@dataclass
class YieldConfig(AgentConfig):
    """Yield agent 专属参数"""

    min_tvl_usd: float = 1_000_000.0     # 流动性下限
    min_apy_pct: float = 0.5             # 低于此不值得
    max_apy_pct: float = 200.0           # 高于此视为噪音/风险
    rebalance_threshold: float = 0.15    # 相对提升 15% 才迁移
    top_n: int = 5                       # 输出前 N 个候选
    current_pool_id: str = ""            # 当前持仓池(空=空仓)


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
        stability = 1.0
        if apy30 > 0:
            deviation = abs(apy - apy30) / apy30
            stability = max(0.3, 1.0 - deviation)

        # 2) 流动性: 低于目标 TVL 线性衰减
        liquidity = min(1.0, tvl / self.config.min_tvl_usd) if tvl else 0.0

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

        if not current_id:
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
