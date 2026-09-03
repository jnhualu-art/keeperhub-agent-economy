"""真上链执行: CapitalEfficiencyAgent -> Executor -> KeeperHub -> Aave V3 borrow

这是项目的**第二条真上链路径**。第一条是 HealthFactorAgent 的 repay
(防守: HF 过低时还债); 这一条是 CapitalEfficiencyAgent 的 borrow
(进攻: HF 过高时释放闲置额度)。两者共用同一个 Executor、同一套风控与
审计管道 —— 这正是「执行层可复用」而非「一次性 demo」的证明。

执行前会重算一次风控, 若链上状态已变化导致 HF 不足则自动中止。

用法:
    python scripts/run_live_borrow.py            # 真发
    python scripts/run_live_borrow.py --dry      # 只算不发
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import env as env_loader  # noqa: E402

env_loader.load()

from capital_efficiency_agent import (  # noqa: E402
    CapitalEfficiencyAgent,
    CapitalEfficiencyConfig,
)
from executor import Executor  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

DRY = "--dry" in sys.argv


def main() -> int:
    print("=" * 72)
    print("Capital Efficiency Rebalance — live Aave V3 borrow via KeeperHub")
    print("=" * 72)

    cfg = CapitalEfficiencyConfig(
        agent_name="capital-efficiency.agent",
        dry_run=DRY,
        network="sepolia",
    )
    agent = CapitalEfficiencyAgent(cfg)
    history = agent.run(cycles=1)

    if not history:
        print("[ABORT] agent 未产出任何快照, 不执行")
        return 1

    snap = history[-1]
    actions = snap.get("actions") or []
    print(f"\n[agent] {snap.get('notes')}")
    print(f"[agent] metrics = {json.dumps(snap.get('metrics'), ensure_ascii=False)}")

    if not actions:
        print("\n[ABORT] agent 判断当前不应借款, 不执行")
        return 0

    # executor 的 dry_run 独立控制是否点火
    executor = Executor(dry_run=DRY)

    print(f"\n[executor] dry_run = {executor.dry_run}")
    records = executor.execute_batch(actions)

    for rec in records:
        print("\n" + "-" * 72)
        print(json.dumps(rec, ensure_ascii=False, indent=2))

        if rec.get("tx_hash"):
            tx = rec["tx_hash"]
            print(f"\n[SUCCESS] tx = {tx}")
            print(f"          https://sepolia.etherscan.io/tx/{tx}")
        elif rec.get("executed"):
            print("\n[SUBMITTED] 已提交, 但未直接返回 tx hash:")
            print(json.dumps(rec.get("plan"), ensure_ascii=False, indent=2))
        elif rec.get("error"):
            print(f"\n[FAILED] {rec['error']}")
            return 1
        else:
            print(f"\n[NOT EXECUTED] {rec.get('note')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
