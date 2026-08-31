"""
Base Agent — ERC-8004 四类 agent 的共享基类
============================================

背景:
  BSC 链上现有 20 万+ agent 几乎全是通用 AI agent(写代码 / 做设计 / 跑广告),
  而 Build the Era 官方要求的四大金融类别近乎空白:
    - Rebalancing          LP 区间再平衡
    - Grid Trading         网格交易 / 做市
    - Yield Optimisation   收益优化路由
    - Health Factor        借贷健康因子监控
  因此本项目自建这四类 reference agent: 真实读链、真实风控、并注册到
  ERC-8004 成为链上可发现资产(marketplace 直接索引自己生产的 agent)。

设计原则(沿用 silent-martin 的生产级风格):
  - 单文件可运行, dry_run 默认开启(绝不误发真实交易)
  - 硬性 kill-switch: 亏损超限 / 数据陈旧 / 连续异常 → 立即停机
  - 结构化状态输出, 供 marketplace 前端实时展示
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 官方四大类别
CATEGORY_REBALANCING = "rebalancing"
CATEGORY_GRID_TRADING = "grid_trading"
CATEGORY_YIELD = "yield_optimisation"
CATEGORY_HEALTH_FACTOR = "health_factor"

CATEGORY_META = {
    CATEGORY_REBALANCING: {
        "label": "Rebalancing",
        "desc": "Manages LP ranges, resets positions automatically",
    },
    CATEGORY_GRID_TRADING: {
        "label": "Grid Trading",
        "desc": "Places and manages automated grid orders",
    },
    CATEGORY_YIELD: {
        "label": "Yield Optimisation",
        "desc": "Routes liquidity to the highest available APR",
    },
    CATEGORY_HEALTH_FACTOR: {
        "label": "Health Factor Monitoring",
        "desc": "Protects lending positions from liquidation",
    },
}


@dataclass
class AgentConfig:
    """运行配置 + 风控参数"""

    network: str = "testnet"          # testnet 默认, 安全且 gasless
    dry_run: bool = True              # 默认不发真实交易
    cycle_interval_sec: int = 60      # 每轮间隔
    max_cycles: int = 0               # 0 = 无限循环

    # ---- 风控 (silent-martin 风格硬约束) ----
    kill_switch_loss_pct: float = 5.0     # 回撤超过此比例立即停机
    max_data_age_sec: int = 180           # 数据陈旧超过此秒数停止决策
    max_consecutive_errors: int = 5       # 连续异常停机阈值
    max_position_usd: float = 10_000.0    # 单 agent 最大名义仓位

    # ---- ERC-8004 身份 ----
    agent_name: str = ""
    agent_description: str = ""
    agent_image: str = ""
    service_endpoint: str = ""            # A2A / MCP 端点


@dataclass
class AgentState:
    """每轮运行的结构化输出, marketplace 直接消费"""

    cycle: int = 0
    timestamp: str = ""
    status: str = "idle"          # idle / running / halted / error
    kill_switch_active: bool = False
    data_age_sec: float = 0.0
    error_count: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class BaseAgent:
    """所有 reference agent 的基类"""

    CATEGORY: str = "uncategorised"

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()
        self.state = AgentState()
        self.history: list[dict] = []
        self._current_data: dict = {}      # 最近一轮的市场数据
        self._running = False

        if not self.config.agent_name:
            self.config.agent_name = self.__class__.__name__

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict:
        """
        执行一轮决策。子类实现。

        返回 dict: {metrics: {...}, actions: [...], notes: "..."}
        """
        raise NotImplementedError

    def fetch_market_data(self) -> dict:
        """拉取本轮所需市场数据。子类实现。"""
        return {}

    # ------------------------------------------------------------------
    # 风控 (核心, 不可绕过)
    # ------------------------------------------------------------------

    def check_risk(self, metrics: dict) -> tuple[bool, str]:
        """
        硬性风控检查。返回 (是否允许继续, 原因)。

        子类可覆写以加入自己的风控, 但必须调用 super()。
        """
        # 1) 亏损 kill-switch
        drawdown = metrics.get("drawdown_pct")
        if drawdown is not None and abs(drawdown) >= self.config.kill_switch_loss_pct:
            return False, f"kill-switch: drawdown {drawdown}% >= {self.config.kill_switch_loss_pct}%"

        # 2) 数据新鲜度
        if self.state.data_age_sec > self.config.max_data_age_sec:
            return False, f"stale data: {self.state.data_age_sec:.0f}s > {self.config.max_data_age_sec}s"

        # 3) 连续异常
        if self.state.error_count >= self.config.max_consecutive_errors:
            return False, f"too many errors: {self.state.error_count}"

        return True, "ok"

    def _record_data_timestamp(self, data: dict) -> None:
        """从数据里提取时间戳, 计算数据年龄"""
        ts = data.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            self.state.data_age_sec = time.time() - ts
        else:
            self.state.data_age_sec = 0.0

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self, cycles: int | None = None) -> list[dict]:
        """
        运行主循环。

        :param cycles: 覆盖 config.max_cycles, None 用配置值
        """
        max_cycles = cycles if cycles is not None else self.config.max_cycles
        self._running = True
        self.state.status = "running"
        logger.info(
            "[%s] start (dry_run=%s, network=%s, max_cycles=%s)",
            self.config.agent_name,
            self.config.dry_run,
            self.config.network,
            max_cycles or "infinite",
        )

        n = 0
        while self._running:
            n += 1
            self.state.cycle = n
            self.state.timestamp = datetime.now(timezone.utc).isoformat()

            try:
                data = self.fetch_market_data()
                self._current_data = data          # 供子类 run_cycle 消费
                self._record_data_timestamp(data)

                result = self.run_cycle()
                self.state.metrics = result.get("metrics", {})
                self.state.actions = result.get("actions", [])
                self.state.notes = result.get("notes", "")
                self.state.error_count = 0

                allowed, reason = self.check_risk(self.state.metrics)
                if not allowed:
                    self.state.kill_switch_active = True
                    self.state.status = "halted"
                    self.state.notes = f"HALTED: {reason}"
                    logger.warning("[%s] %s", self.config.agent_name, self.state.notes)
                    break

                self.state.status = "running"

            except Exception as exc:
                self.state.error_count += 1
                self.state.status = "error"
                self.state.notes = f"error: {exc}"
                logger.exception("[%s] cycle %s failed", self.config.agent_name, n)
                if self.state.error_count >= self.config.max_consecutive_errors:
                    break

            snapshot = self.state.to_dict()
            self.history.append(snapshot)

            # 每轮打印摘要, 便于 marketplace 与演示
            logger.info(
                "[%s] cycle=%s status=%s actions=%s %s",
                self.config.agent_name,
                n,
                self.state.status,
                len(self.state.actions),
                json.dumps(self.state.metrics, ensure_ascii=False)[:200],
            )

            if max_cycles and n >= max_cycles:
                break
            if self.config.cycle_interval_sec:
                time.sleep(self.config.cycle_interval_sec)

        self._running = False
        return self.history

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # ERC-8004 身份
    # ------------------------------------------------------------------

    def to_registration_file(self) -> dict:
        """
        生成符合 ERC-8004 的 registration file JSON。

        结构对齐 TermiX 平台实测格式, 确保能被 8004scan 与本项目
        marketplace 索引器正确解析与分类。
        """
        meta = CATEGORY_META.get(self.CATEGORY, {"label": "Uncategorised", "desc": ""})
        services = []
        if self.config.service_endpoint:
            services.append(
                {
                    "name": "A2A",
                    "endpoint": self.config.service_endpoint,
                    "version": "0.3.0",
                }
            )

        return {
            "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
            "name": self.config.agent_name,
            "description": self.config.agent_description or meta["desc"],
            "image": self.config.agent_image,
            "services": services,
            "x402Support": True,
            "active": True,
            "registrations": [],
            "supportedTrust": ["reputation"],
            "tags": [meta["label"]],
            "category": self.CATEGORY,
            "categoryLabel": meta["label"],
            "attributes": [{"trait_type": "category", "value": meta["label"]}],
        }

    def current_status(self) -> dict:
        """marketplace 前端消费的实时状态"""
        meta = CATEGORY_META.get(self.CATEGORY, {"label": "Uncategorised", "desc": ""})
        return {
            "name": self.config.agent_name,
            "category": self.CATEGORY,
            "category_label": meta["label"],
            "description": self.config.agent_description or meta["desc"],
            "network": self.config.network,
            "dry_run": self.config.dry_run,
            "state": self.state.to_dict(),
            "history_size": len(self.history),
        }
