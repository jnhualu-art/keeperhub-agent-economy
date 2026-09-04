"""
Rebalancing Agent
=================
官方类别: Rebalancing — "Manages LP ranges, resets positions automatically"

真实链上实现: 直接读 PancakeSwap V3 集中流动性仓位, 检测是否脱离价格区间。

数据流(全部链上, 无 mock):
  1. NonfungiblePositionManager.balanceOf(owner)        -> 用户 LP NFT 数量
  2. tokenOfOwnerByIndex(owner, i)                      -> tokenId
  3. positions(tokenId)                                 -> (tickLower, tickUpper, liquidity, fee...)
  4. Factory.getPool(token0, token1, fee)               -> 池子地址
  5. Pool.slot0()                                       -> 当前 tick / sqrtPriceX96
  6. 判定:
        in_range = tickLower <= currentTick < tickUpper
        脱离区间 -> 该仓位已停止赚取手续费, 且单边暴露全部风险资产
  7. 建议新区间(按配置的宽度, 并对齐 tickSpacing), 输出 rebalance 动作

价格换算(Uniswap V3 标准):
       price = 1.0001 ** tick
       tick  = ln(price) / ln(1.0001)
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field

# 允许从 backend/ 与 backend/agents/ 两个层级导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _web3():
    """惰性导入 web3 —— 理由同 grid_agent._web3()。

    tick 对齐与区间计算是纯算术, 不该被一条坏掉的 web3 依赖链挡在可测
    范围之外。只在真正发起 RPC 时才要求 web3 可用。
    """
    try:
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError(
            "web3 不可用, 无法发起链上调用(区间计算本身不需要 web3)"
        ) from exc
    return Web3


from base_agent import (
    CATEGORY_REBALANCING,
    AgentConfig,
    BaseAgent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PancakeSwap V3 — BSC 主网地址(已验证存在合约代码)
# ---------------------------------------------------------------------------

PANCAKE_V3_POSITION_MANAGER = "0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613"
# Factory 地址一律从 PositionManager.factory() 动态读取。
# 实测坑: BSC 上该 PositionManager 下大量仓位是 fee=3000(Uniswap V3 的 0.3% tier),
# 而 PancakeSwap V3 用的是 2500 —— 硬编码 PancakeSwap Factory 会查不到池子返回零地址。
# 动态读取可同时兼容 Uniswap V3 / PancakeSwap V3 部署。

BSC_RPCS = [
    "https://bsc.publicnode.com",
    "https://1rpc.io/bnb",
]

POSITION_MANAGER_ABI = [
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

FACTORY_ABI = [
    {
        "inputs": [
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "tickSpacing",
        "outputs": [{"name": "", "type": "int24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

TICK_BASE = 1.0001


@dataclass
class RebalancingConfig(AgentConfig):
    """Rebalancing agent 专属参数"""

    monitored_address: str = ""      # LP 持有者地址
    token_ids: list = field(default_factory=list)   # 直接指定 tokenId(跳过遍历)
    range_width_pct: float = 10.0    # 新区间宽度(单边 %), 如 10 表示 ±10%
    max_positions: int = 100         # 最多扫描多少个 NFT
    max_active: int = 10             # 最多保留多少个活跃仓位(liquidity>0)
    rebalance_band_pct: float = 2.0  # 价格贴近边界多少 % 以内就预警(未脱区间先提示)
    rpc_url: str = ""
    rpc_throttle_sec: float = 0.35


def tick_to_price(tick: int) -> float:
    """Uniswap V3: price = 1.0001 ** tick"""
    return TICK_BASE ** tick


def price_to_tick(price: float) -> int:
    """Uniswap V3: tick = ln(price) / ln(1.0001)"""
    if price <= 0:
        raise ValueError("price must be positive")
    return int(math.log(price) / math.log(TICK_BASE))


def align_to_spacing(tick: int, spacing: int) -> int:
    """把 tick 对齐到池子的 tickSpacing(链上要求必须是 spacing 的整数倍)。

    向下对齐(朝 -inf)。保留此函数仅为兼容既有调用方; 新代码应显式选择
    方向 —— 见 align_down / align_up, 方向选错会直接改变区间大小。
    """
    if not spacing:
        return tick
    return (tick // spacing) * spacing


def align_down(tick: int, spacing: int) -> int:
    """向下对齐 —— 用于区间下界: 只会让区间变宽, 不会把现价挤出区间。"""
    if not spacing:
        return tick
    return (tick // spacing) * spacing


def align_up(tick: int, spacing: int) -> int:
    """向上对齐 —— 用于区间上界。

    上界必须向上对齐: 向下对齐会缩窄区间, 实测在 range_width_pct=0.001
    这类极窄配置下上下界会被压到同一个 tick, 产生空区间 —— 链上必然失败,
    而且失败前从数据上看不出任何异常。
    """
    if not spacing:
        return tick
    return -((-tick) // spacing) * spacing


class RebalancingAgent(BaseAgent):
    CATEGORY = CATEGORY_REBALANCING

    def __init__(self, config: RebalancingConfig | None = None):
        super().__init__(config or RebalancingConfig())
        self.config: RebalancingConfig

        from erc8004 import make_web3, pick_rpc

        self.rpc_url = self.config.rpc_url or pick_rpc(BSC_RPCS)
        self.w3 = make_web3(self.rpc_url, timeout=25)
        self.pm = self.w3.eth.contract(
            address=_web3().to_checksum_address(PANCAKE_V3_POSITION_MANAGER),
            abi=POSITION_MANAGER_ABI,
        )
        # factory 动态解析: 兼容 Uniswap V3 与 PancakeSwap V3 部署
        self.factory_address = self.pm.functions.factory().call()
        self.factory = self.w3.eth.contract(
            address=_web3().to_checksum_address(self.factory_address), abi=FACTORY_ABI
        )
        self._symbol_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 数据层
    # ------------------------------------------------------------------

    def fetch_market_data(self) -> dict:
        # 模式 A: 直接指定 tokenId — 活跃 LP 机器人常持有成百上千个历史空 NFT,
        #         遍历 owner 既慢又难命中, 直接给 tokenId 才靠谱
        if self.config.token_ids:
            positions = []
            for tid in self.config.token_ids:
                try:
                    pos = self._read_position(int(tid))
                    if pos:
                        positions.append(pos)
                except Exception as exc:
                    logger.warning("read tokenId %s failed: %s", tid, exc)
                if self.config.rpc_throttle_sec:
                    time.sleep(self.config.rpc_throttle_sec)

            return {
                "timestamp": time.time(),
                "positions": positions,
                "nft_count": len(self.config.token_ids),
                "scanned": len(self.config.token_ids),
                "mode": "token_ids",
            }

        # 模式 B: 遍历持有者的全部 NFT
        if not self.config.monitored_address:
            raise ValueError("必须设置 monitored_address 或 token_ids")

        owner = _web3().to_checksum_address(self.config.monitored_address)
        n = self.pm.functions.balanceOf(owner).call()
        n = min(n, self.config.max_positions)

        positions = []
        scanned = 0
        for i in range(n):
            # 持有者常留大量已撤出流动性的空 NFT(liquidity=0),
            # 必须继续往下找, 直到收集够活跃仓位
            if len(positions) >= self.config.max_active:
                break
            scanned += 1
            try:
                token_id = self.pm.functions.tokenOfOwnerByIndex(owner, i).call()
                pos = self._read_position(token_id)
                if pos:
                    positions.append(pos)
            except Exception as exc:
                logger.warning("read position #%s failed: %s", i, exc)

            if self.config.rpc_throttle_sec:
                time.sleep(self.config.rpc_throttle_sec)

        return {
            "timestamp": time.time(),
            "positions": positions,
            "nft_count": n,
            "scanned": scanned,
        }

    def _read_position(self, token_id: int) -> dict | None:
        p = self.pm.functions.positions(token_id).call()
        (_nonce, _operator, token0, token1, fee, tick_lower, tick_upper, liquidity,
         _fg0, _fg1, owed0, owed1) = p

        if liquidity == 0:
            return None

        pool_addr = self.factory.functions.getPool(token0, token1, fee).call()
        if pool_addr == "0x" + "0" * 40:
            return None

        pool_cs = _web3().to_checksum_address(pool_addr)
        pool = self.w3.eth.contract(address=pool_cs, abi=POOL_ABI)
        slot0 = pool.functions.slot0().call()
        current_tick = slot0[1]
        spacing = pool.functions.tickSpacing().call()

        sym0 = self._symbol(token0)
        sym1 = self._symbol(token1)

        price = tick_to_price(current_tick)
        price_lower = tick_to_price(tick_lower)
        price_upper = tick_to_price(tick_upper)

        in_range = tick_lower <= current_tick < tick_upper

        # 距上下边界的余量(%)
        headroom_up = (price_upper - price) / price * 100 if price else 0
        headroom_down = (price - price_lower) / price * 100 if price else 0

        return {
            "token_id": token_id,
            "pair": f"{sym0}/{sym1}",
            "fee_tier": fee,
            "fee_pct": fee / 10000,
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "current_tick": current_tick,
            "tick_spacing": spacing,
            "liquidity": liquidity,
            "price": price,
            "price_lower": price_lower,
            "price_upper": price_upper,
            "in_range": in_range,
            "headroom_up_pct": round(headroom_up, 3),
            "headroom_down_pct": round(headroom_down, 3),
            "uncollected_fees": [owed0, owed1],
            "pool": pool_cs,
        }

    def _symbol(self, token_addr: str) -> str:
        cs = _web3().to_checksum_address(token_addr)
        if cs in self._symbol_cache:
            return self._symbol_cache[cs]
        try:
            sym = self.w3.eth.contract(address=cs, abi=ERC20_ABI).functions.symbol().call()
        except Exception:
            sym = cs[:8]
        self._symbol_cache[cs] = sym
        return sym

    # ------------------------------------------------------------------
    # 策略核心
    # ------------------------------------------------------------------

    def propose_new_range(self, pos: dict) -> dict:
        """按当前价格与配置宽度, 计算并对齐后的新区间"""
        width = self.config.range_width_pct / 100.0
        price = pos["price"]

        lower_price = price * (1 - width)
        upper_price = price * (1 + width)

        spacing = pos["tick_spacing"]
        # 下界向下、上界向上: 两个方向都是「往外扩」, 保证现价仍在区间内。
        new_lower = align_down(price_to_tick(lower_price), spacing)
        new_upper = align_up(price_to_tick(upper_price), spacing)

        # 兜底: 配置宽度小于一个 tickSpacing 时, 即便双向外扩, 上下界仍可能
        # 撞在一起(如 width=0.001% 时 46053/46054 都对齐到 46050)。空区间
        # 上链必然 revert, 宁可把区间撑到最小可用宽度。
        widened = False
        if new_upper <= new_lower:
            new_upper = new_lower + spacing
            widened = True

        return {
            "new_tick_lower": new_lower,
            "new_tick_upper": new_upper,
            "new_price_lower": tick_to_price(new_lower),
            "new_price_upper": tick_to_price(new_upper),
            "width_pct": self.config.range_width_pct,
            "widened_to_min_width": widened,
        }

    def run_cycle(self) -> dict:
        positions = self._current_data.get("positions", [])
        if not positions:
            return {
                "metrics": {"positions": 0},
                "actions": [],
                "notes": f"no PancakeSwap V3 LP position for {self.config.monitored_address}",
            }

        actions = []
        out_of_range = [p for p in positions if not p["in_range"]]
        near_edge = [
            p for p in positions
            if p["in_range"]
            and min(p["headroom_up_pct"], p["headroom_down_pct"]) <= self.config.rebalance_band_pct
        ]

        for p in out_of_range:
            proposal = self.propose_new_range(p)
            actions.append(
                {
                    "type": "REBALANCE",
                    "priority": "HIGH",
                    "token_id": p["token_id"],
                    "pair": p["pair"],
                    "reason": "out of range - earning zero fees, full single-sided exposure",
                    "current_tick": p["current_tick"],
                    "range": [p["tick_lower"], p["tick_upper"]],
                    **proposal,
                    "dry_run": self.config.dry_run,
                }
            )

        for p in near_edge:
            proposal = self.propose_new_range(p)
            actions.append(
                {
                    "type": "PREPOSITION",
                    "priority": "MEDIUM",
                    "token_id": p["token_id"],
                    "pair": p["pair"],
                    "reason": f"price within {self.config.rebalance_band_pct}% of range edge",
                    "headroom_up_pct": p["headroom_up_pct"],
                    "headroom_down_pct": p["headroom_down_pct"],
                    **proposal,
                    "dry_run": self.config.dry_run,
                }
            )

        in_range = len(positions) - len(out_of_range) - len(near_edge)
        notes = (
            f"{len(out_of_range)} out-of-range, {len(near_edge)} near edge, "
            f"{in_range} healthy"
        )

        metrics = {
            "positions": len(positions),
            "out_of_range": len(out_of_range),
            "near_edge": len(near_edge),
            "healthy": max(0, in_range),
            "pairs": [p["pair"] for p in positions[:5]],
        }

        return {"metrics": metrics, "actions": actions, "notes": notes, "positions": positions}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    addr = os.getenv("MONITOR_ADDRESS", "")
    token_ids = [int(x) for x in os.getenv("TOKEN_IDS", "").split(",") if x.strip()]
    if not addr and not token_ids:
        print("请设置 MONITOR_ADDRESS 或 TOKEN_IDS 环境变量")
        print("示例: TOKEN_IDS=2690498,2690499 python rebalancing_agent.py")
        return

    cfg = RebalancingConfig(
        agent_name="rangeguard.agent",
        agent_description=(
            "Monitors PancakeSwap V3 concentrated-liquidity positions on BSC and "
            "keeps them in range. Reads live position ticks and pool slot0, detects "
            "out-of-range (zero fee accrual, full single-sided exposure), and proposes "
            "a new range aligned to the pool tickSpacing."
        ),
        monitored_address=addr,
        token_ids=token_ids,
        dry_run=True,
        network="mainnet",
        cycle_interval_sec=0,
    )

    agent = RebalancingAgent(cfg)
    agent.run(cycles=1)

    print("\n" + "=" * 70)
    print("FINAL STATUS")
    print("=" * 70)
    print(json.dumps(agent.current_status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
