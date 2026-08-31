"""
KeeperHub — The Agent Economy Hackathon 整合入口
=================================================

把 BNB Agent Studio 四大金融决策 agent (真实链上读 + 真实风控) 与
rebalance-keeper 的 KeeperHub 真上链执行层整合为一个「Agent 经济」舰队:

  1. HealthFactorAgent  (Aave V3 健康因子保护)  -> PROTECT  -> aave-v3/repay 真上链
  2. RebalancingAgent   (PancakeSwap V3 LP 再平衡) -> REBALANCE -> contract call plan
  3. YieldOptimisationAgent (BSC 收益路由)       -> ENTER/MIGRATE -> contract call plan
  4. GridTradingAgent   (BSC 网格做市)           -> QUOTE -> DEX 报价 plan

运行流:
  build_fleet -> 逐 agent run (捞 actions) -> executor 路由到 KeeperHub -> 审计落盘

安全: dry_run 默认开启, 无 API Key 或无 wallet 时绝不真发交易; 只有
      PROTECT 在 live 模式下经 KeeperHub 真还债 (Aave V3)。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from base_agent import BaseAgent, AgentConfig
from executor import Executor

# 决策 agent — health_factor 仅依赖标准库, 始终可用
from health_factor_agent import HealthFactorAgent, HealthFactorConfig

logger = logging.getLogger(__name__)

# 其余三个 agent 依赖 web3 / httpx, 缺失时优雅降级 (仍可跑核心整合)
try:
    from rebalancing_agent import RebalancingAgent, RebalancingConfig
except Exception as _e:
    RebalancingAgent = None
    logger.warning("rebalancing_agent 不可用 (缺 web3): %s", _e)

try:
    from yield_agent import YieldOptimisationAgent, YieldConfig
except Exception as _e:
    YieldOptimisationAgent = None
    logger.warning("yield_agent 不可用 (缺 httpx): %s", _e)

try:
    from grid_agent import GridTradingAgent, GridConfig
except Exception as _e:
    GridTradingAgent = None
    logger.warning("grid_agent 不可用 (缺 web3): %s", _e)


STATUS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "fleet_status.json",
)


# ---------------------------------------------------------------------------
# 舰队构建
# ---------------------------------------------------------------------------

def build_fleet() -> Tuple[List[Tuple[str, BaseAgent]], List[str]]:
    """构建四大决策 agent 舰队。每个 agent 独立可失败。

    返回 (fleet, skipped_deps): skipped_deps 为因缺依赖/web3 而跳过的 agent 名。
    """
    dry = os.getenv("DRY_RUN", "true").lower() != "false"

    fleet: List[Tuple[str, BaseAgent]] = []
    skipped_deps: List[str] = []

    # 1) Health Factor — Aave V3 (always runnable, mock fallback)
    hf = HealthFactorAgent(HealthFactorConfig(
        agent_name="hfsentinel.agent",
        agent_description=(
            "Monitors Aave V3 lending positions and protects them from liquidation. "
            "Reads live on-chain account data via KeeperHub MCP, computes health factor, "
            "and emits a repay action executed on-chain through KeeperHub (no custodial key)."
        ),
        monitored_address=os.getenv("MONITOR_ADDRESS", ""),
        dry_run=dry,
        network="sepolia",
        cycle_interval_sec=0,
    ))
    fleet.append(("HealthFactor", hf))

    # 2) Rebalancing — PancakeSwap V3 (needs web3)
    if RebalancingAgent is None:
        skipped_deps.append("Rebalancing")
    else:
        rb_cfg = RebalancingConfig(
            agent_name="rangeguard.agent",
            agent_description=(
                "Monitors PancakeSwap V3 concentrated-liquidity positions on BSC and keeps "
                "them in range. Reads live position ticks and pool slot0, detects out-of-range "
                "(zero fee accrual, full single-sided exposure), and proposes a new range."
            ),
            monitored_address=os.getenv("MONITOR_ADDRESS", ""),
            token_ids=[int(x) for x in os.getenv("TOKEN_IDS", "").split(",") if x.strip()],
            dry_run=dry,
            network="mainnet",
            cycle_interval_sec=0,
        )
        fleet.append(("Rebalancing", RebalancingAgent(rb_cfg)))

    # 3) Yield — BSC DefiLlama (needs httpx)
    if YieldOptimisationAgent is None:
        skipped_deps.append("Yield")
    else:
        yld = YieldOptimisationAgent(YieldConfig(
            agent_name="yieldpilot.agent",
            agent_description=(
                "Routes stablecoin liquidity across BSC pools by risk-adjusted APY. Filters "
                "noise pools via TVL floor and APY sanity bounds, scores candidates by "
                "APY x stability x liquidity, and migrates only when uplift covers gas."
            ),
            dry_run=dry,
            network="mainnet",
            cycle_interval_sec=0,
        ))
        fleet.append(("Yield", yld))

    # 4) Grid — BSC market making (needs web3)
    if GridTradingAgent is None:
        skipped_deps.append("Grid")
    else:
        grid = GridTradingAgent(GridConfig(
            agent_name="silent-martin.agent",
            agent_description=(
                "BSC port of silent-martin, a Hummingbot Botcamp CERTIFIED market-making "
                "strategy. Anchors every quote to on-chain DEX pool price, widens spread on "
                "dislocation, applies inventory skew, sizes by ATR, halts on extreme dislocation."
            ),
            dry_run=dry,
            network="mainnet",
            cycle_interval_sec=0,
        ))
        fleet.append(("Grid", grid))

    return fleet, skipped_deps


# ---------------------------------------------------------------------------
# 运行 + 路由
# ---------------------------------------------------------------------------

def run_fleet(fleet: List[Tuple[str, BaseAgent]]) -> Tuple[List[Dict], List[Dict]]:
    """
    逐 agent 跑一轮, 收集所有 action。返回 (agent_reports, all_actions)。
    单个 agent 失败不影响其余。
    """
    agent_reports: List[Dict] = []
    all_actions: List[Dict] = []

    for name, agent in fleet:
        report = {
            "name": name,
            "agent_name": agent.config.agent_name,
            "category": agent.CATEGORY,
            "status": "ok",
            "error": None,
            "metrics": {},
            "actions": [],
            "notes": "",
        }
        try:
            # Rebalancing 无 address/token_ids 时 fetch_market_data 会抛错 -> 标记需配置
            history = agent.run(cycles=1)
            if history:
                snap = history[-1]
                report["metrics"] = snap.get("metrics", {})
                report["actions"] = snap.get("actions", [])
                report["notes"] = snap.get("notes", "")
            for a in report["actions"]:
                a.setdefault("source_agent", name)
                all_actions.append(a)
        except Exception as exc:
            report["status"] = "skipped"
            report["error"] = str(exc)
            logger.warning("[%s] run skipped: %s", name, exc)

        agent_reports.append(report)
        logger.info(
            "[%s] status=%s actions=%d", name, report["status"], len(report["actions"])
        )

    return agent_reports, all_actions


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dry = os.getenv("DRY_RUN", "true").lower() != "false"
    logger.info("=" * 70)
    logger.info("KeeperHub Agent Economy — fleet boot (dry_run=%s)", dry)
    logger.info("=" * 70)

    fleet, skipped_deps = build_fleet()
    agent_reports, all_actions = run_fleet(fleet)

    # 路由到 KeeperHub 执行层
    executor = Executor(dry_run=dry)
    exec_records = executor.execute_batch(all_actions)

    # 汇总
    executed = [r for r in exec_records if r.get("executed")]
    planned = [r for r in exec_records if not r.get("executed") and r.get("plan")]
    skipped = [r for r in exec_records if not r.get("plan") and not r.get("executed")]

    summary = {
        "timestamp": time.time(),
        "dry_run": dry,
        "agents": len(fleet),
        "agents_ok": sum(1 for r in agent_reports if r["status"] == "ok"),
        "skipped_deps": skipped_deps,
        "total_actions": len(all_actions),
        "executed_onchain": len(executed),
        "planned": len(planned),
        "skipped": len(skipped),
        "agent_reports": agent_reports,
        "execution_records": exec_records,
    }

    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    # 控制台报告
    print("\n" + "=" * 70)
    print("KEEPERHUB AGENT ECONOMY — FLEET REPORT")
    print("=" * 70)
    print(f"dry_run            : {dry}")
    print(f"agents             : {summary['agents']} ({summary['agents_ok']} ok)")
    if skipped_deps:
        print(f"skipped (no dep)   : {', '.join(skipped_deps)} (pip install -r requirements.txt)")
    print(f"total actions      : {summary['total_actions']}")
    print(f"executed on-chain  : {summary['executed_onchain']}")
    print(f"planned (audit)    : {summary['planned']}")
    print(f"skipped            : {summary['skipped']}")
    print("-" * 70)
    for r in agent_reports:
        print(f"[{r['name']:>11}] {r['status']:>7}  actions={len(r['actions'])}  {r['notes'][:48]}")
    print("-" * 70)
    for rec in exec_records:
        tag = "ONCHAIN" if rec.get("executed") else ("PLAN" if rec.get("plan") else "SKIP")
        print(f"  {tag:>7} {rec['type']:<10} {rec.get('note', '')[:60]}")
    print("=" * 70)
    print(f"full report -> {STATUS_PATH}")
    if dry:
        print("提示: 设 DRY_RUN=false + KEEPERHUB_API_KEY 后, PROTECT 将真上链还债 (Aave V3)")


if __name__ == "__main__":
    main()
