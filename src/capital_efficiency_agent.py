"""
Capital Efficiency Agent — Rebalancing 类别的第二个专业化 agent
===============================================================

解决的问题: 抵押借贷仓位里「沉睡的借款额度」。

现状是绝大多数自管仓位的常态 —— 用户为了安全过度抵押, 结果大笔借款额度
一直躺着, 既不产生收益也不被使用。以本项目监控的 Sepolia 仓位为例:

    抵押 200.00 USD | 负债 119.48 USD | 可借 40.52 USD | HF 1.3810

清算线是 HF = 1.0, 而仓位常年停在 1.38 —— 这中间是**被闲置的安全垫**。
本 agent 做的事: 在**严格保住最低健康因子**的前提下, 把这部分闲置额度
借出来, 让死抵押变成活资本。

风控模型 (这是本 agent 的核心, 不是无脑借满)
---------------------------------------------
    HF = (collateral * liquidation_threshold) / debt

反解出「借到某个债务水平时 HF 会是多少」, 就能算出在不跌破安全线的前提下
还能借多少:

    max_debt   = collateral * liq_threshold / HF_target
    max_borrow = (max_debt - current_debt) * SAFETY_FACTOR

再与链上真实可借额度 (availableBorrowsBase) 取 min, 并设最小金额门槛,
避免为了几美分去付一笔 gas。

三重约束缺一不可:
  1. HF_target   借款后 HF 必须仍高于此值 (默认 1.30, 远高于清算线 1.0)
  2. SAFETY      在理论上限上再打折留缓冲 (默认 0.90)
  3. chain cap   链上 availableBorrows 是硬顶, 借不出来就不能借

若 max_borrow 算出来 <= 0 (说明 HF 已经贴着甚至低于安全线), 本 agent
**不会**建议借款 —— 那是 HealthFactorAgent 该管的防守场景, 两个 agent
职责边界清晰, 互不越界。

数据流 (全部链上真实读取, 无 mock)
-----------------------------------
    aave-v3/get-user-account-data  ->  collateral / debt / available / threshold / HF
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as app_config
from base_agent import (
    CATEGORY_REBALANCING,
    AgentConfig,
    BaseAgent,
)
from keeperhub_client import KeeperHubClient, MCPError

# 注意: 这里刻意**不**在模块级加载 .env。config 在 import 时就快照了环境变量,
# 模块级注入会让单元测试拿到真实 API Key 从而误连网络。需要读真实环境的入口
# (CLI / 脚本) 请自行先调用 env.load(), 例如 run_live_borrow.py 的开头。

logger = logging.getLogger(__name__)

# Aave 用 uint256.max 表示「无债务」时的健康因子
_UINT256_MAX = 2 ** 256 - 1


@dataclass
class CapitalEfficiencyConfig(AgentConfig):
    """资本效率再平衡 agent 的专属参数"""

    # 借款后必须保住的最低健康因子。清算线是 1.0, 1.30 是留足缓冲的保守值。
    hf_target: float = 1.30
    # HF 高于此值才认为「安全垫过厚、资本闲置」, 值得动用额度。
    hf_idle_threshold: float = 1.35
    # 在理论可借上限上再打的折扣, 用于吸收预言机漂移与利息累积。
    safety_factor: float = 0.90
    # 低于此金额不值得发一笔交易 (cover gas / 噪音过滤)
    min_borrow_usd: float = 1.0
    borrow_asset: str = "USDC"
    interest_rate_mode: str = "2"   # Aave V3: 1=stable, 2=variable
    monitor_address: str = ""       # 空 = 用 WALLET_ADDRESS


def _normalize_hf(raw: int) -> float:
    """Aave 的健康因子是 1e18 精度的整数; uint256.max 代表「无债务」"""
    if raw >= _UINT256_MAX:
        return float("inf")
    return raw / 1e18


def compute_max_borrow(
    collateral_usd: float,
    debt_usd: float,
    liquidation_threshold: float,
    available_usd: float,
    hf_target: float,
    safety_factor: float,
) -> Dict[str, float]:
    """
    在保住 HF >= hf_target 的前提下, 计算还能借出多少。

    这是纯函数 (无 IO), 便于单测覆盖各种边界。

    :returns: dict, 含 max_debt / headroom / borrow / projected_hf
    """
    if collateral_usd <= 0 or liquidation_threshold <= 0:
        return {
            "max_debt": 0.0,
            "headroom": 0.0,
            "borrow": 0.0,
            "projected_hf": float("inf") if debt_usd <= 0 else 0.0,
        }

    # 反解: 要让 HF = hf_target, 债务最多能到多少
    max_debt = collateral_usd * liquidation_threshold / hf_target

    # 理论剩余额度, 打折留缓冲
    headroom = (max_debt - debt_usd) * safety_factor

    # 链上可借额度是硬顶
    borrow = max(0.0, min(headroom, available_usd))

    projected_debt = debt_usd + borrow
    projected_hf = (
        float("inf")
        if projected_debt <= 0
        else collateral_usd * liquidation_threshold / projected_debt
    )

    return {
        "max_debt": max_debt,
        "headroom": headroom,
        "borrow": borrow,
        "projected_hf": projected_hf,
    }


class CapitalEfficiencyAgent(BaseAgent):
    """把过度抵押仓位里闲置的借款额度安全地释放出来。"""

    CATEGORY = CATEGORY_REBALANCING

    def __init__(
        self,
        config: CapitalEfficiencyConfig | None = None,
        client: Optional[KeeperHubClient] = None,
    ):
        super().__init__(config or CapitalEfficiencyConfig())
        self.config: CapitalEfficiencyConfig
        self._client = client
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 依赖注入: 测试时可传入假 client, 避免打网络
    # ------------------------------------------------------------------

    def _get_client(self) -> Optional[KeeperHubClient]:
        if self._client is not None:
            return self._client
        try:
            self._client = KeeperHubClient()
        except Exception as exc:  # 无 Key / 网络异常 -> 降级为无数据
            self._last_error = str(exc)
            logger.warning("KeeperHubClient unavailable: %s", exc)
            return None
        return self._client

    # ------------------------------------------------------------------
    # 数据层: 真实链上读取
    # ------------------------------------------------------------------

    def fetch_market_data(self) -> Dict[str, Any]:
        address = self.config.monitor_address or app_config.WALLET_ADDRESS
        client = self._get_client()
        if client is None:
            return {
                "timestamp": time.time(),
                "available": False,
                "reason": self._last_error or "no KeeperHub client",
            }

        try:
            raw = client.get_user_account_data(address)
        except MCPError as exc:
            self._last_error = str(exc)
            logger.error("get_user_account_data failed: %s", exc)
            return {
                "timestamp": time.time(),
                "available": False,
                "reason": str(exc),
            }
        except Exception as exc:  # 兜底, 单轮失败不应打挂主循环
            self._last_error = str(exc)
            return {
                "timestamp": time.time(),
                "available": False,
                "reason": str(exc),
            }

        # Aave 的 base currency 统一 8 位小数; 阈值是 4 位 (8250 = 82.50%)
        try:
            collateral = int(raw["totalCollateralBase"]) / 1e8
            debt = int(raw["totalDebtBase"]) / 1e8
            available = int(raw["availableBorrowsBase"]) / 1e8
            threshold = int(raw["currentLiquidationThreshold"]) / 1e4
            ltv = int(raw["ltv"]) / 1e4
            hf = _normalize_hf(int(raw["healthFactor"]))
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "timestamp": time.time(),
                "available": False,
                "reason": f"malformed account data: {exc}",
            }

        return {
            "timestamp": time.time(),
            "available": True,
            "address": address,
            "collateral_usd": collateral,
            "debt_usd": debt,
            "available_borrow_usd": available,
            "liquidation_threshold": threshold,
            "ltv": ltv,
            "health_factor": hf,
        }

    # ------------------------------------------------------------------
    # 决策层
    # ------------------------------------------------------------------

    def run_cycle(self) -> Dict[str, Any]:
        d = self._current_data or {}

        if not d.get("available"):
            return {
                "metrics": {"read_ok": False},
                "actions": [],
                "notes": f"no on-chain data: {d.get('reason', 'unknown')}",
            }

        hf = d["health_factor"]
        collateral = d["collateral_usd"]
        debt = d["debt_usd"]
        available = d["available_borrow_usd"]
        threshold = d["liquidation_threshold"]

        calc = compute_max_borrow(
            collateral_usd=collateral,
            debt_usd=debt,
            liquidation_threshold=threshold,
            available_usd=available,
            hf_target=self.config.hf_target,
            safety_factor=self.config.safety_factor,
        )

        metrics = {
            "read_ok": True,
            "health_factor": round(hf, 4),
            "collateral_usd": round(collateral, 2),
            "debt_usd": round(debt, 2),
            "available_borrow_usd": round(available, 2),
            "headroom_usd": round(calc["headroom"], 4),
            "projected_hf": (
                None if calc["projected_hf"] == float("inf") else round(calc["projected_hf"], 4)
            ),
        }

        # ---- 情形 1: 仓位已贴着安全线, 不该再借 ----
        if hf <= self.config.hf_target:
            return {
                "metrics": metrics,
                "actions": [],
                "notes": (
                    f"HF {hf:.4f} <= target {self.config.hf_target} -> "
                    "no headroom; deferred to HealthFactorAgent"
                ),
            }

        # ---- 情形 2: 安全垫还没厚到值得动用额度 ----
        if hf < self.config.hf_idle_threshold:
            return {
                "metrics": metrics,
                "actions": [],
                "notes": (
                    f"HF {hf:.4f} below idle threshold {self.config.hf_idle_threshold} "
                    "-> hold, position already reasonably utilised"
                ),
            }

        borrow = calc["borrow"]

        # ---- 情形 3: 算出来太少, 不值得发交易 ----
        if borrow < self.config.min_borrow_usd:
            return {
                "metrics": metrics,
                "actions": [],
                "notes": (
                    f"headroom {borrow:.4f} USD below min {self.config.min_borrow_usd} "
                    "-> skip (not worth gas)"
                ),
            }

        # 向下取整到分, 避免浮点精度导致链上金额与预期不符
        borrow = int(borrow * 100) / 100
        decimals = app_config.token_decimals(self.config.borrow_asset)
        amount_base = str(int(round(borrow * (10 ** decimals))))

        action = {
            "type": "REBALANCE",
            "venue": "aave-v3",
            "sub_action": "borrow",
            "asset": self.config.borrow_asset,
            "amount_usd": borrow,
            "amount_base": amount_base,
            "interest_rate_mode": self.config.interest_rate_mode,
            "hf_before": round(hf, 4),
            "hf_after": round(calc["projected_hf"], 4),
            "hf_target": self.config.hf_target,
            "rationale": (
                f"HF {hf:.4f} > idle threshold {self.config.hf_idle_threshold}: "
                f"{available:.2f} USD of borrowing power sits idle. "
                f"Borrow {borrow:.2f} {self.config.borrow_asset} while keeping HF "
                f"at {calc['projected_hf']:.4f} (>= target {self.config.hf_target})."
            ),
            "dry_run": self.config.dry_run,
        }

        return {
            "metrics": {**metrics, "borrow_usd": borrow},
            "actions": [action],
            "notes": (
                f"borrow {borrow:.2f} {self.config.borrow_asset} -> "
                f"HF {hf:.4f} to {calc['projected_hf']:.4f}"
            ),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    import importlib
    import json

    # config 在 import 时已快照环境变量, 所以先注入 .env 再 reload,
    # 否则读到的会是空值。
    import env as env_loader

    if env_loader.load():
        importlib.reload(app_config)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = CapitalEfficiencyConfig(
        agent_name="capital-efficiency.agent",
        agent_description=(
            "Frees up idle borrowing power in over-collateralised Aave V3 "
            "positions. Solves for the borrow size that keeps health factor "
            "above a hard floor, then applies a safety discount and the "
            "on-chain borrow cap before proposing any action."
        ),
        dry_run=True,
        network="sepolia",
        cycle_interval_sec=0,
    )

    agent = CapitalEfficiencyAgent(cfg)
    agent.run(cycles=1)

    print("\n" + "=" * 70)
    print("FINAL STATUS")
    print("=" * 70)
    print(json.dumps(agent.current_status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
