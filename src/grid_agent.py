"""
Grid Trading / Market Making Agent — silent-martin BSC 版
=========================================================
官方类别: Grid Trading — "Places and manages automated grid orders"

这是 silent-martin(Hummingbot Botcamp CERTIFIED 策略)向 BSC 的移植版。
原策略以 Flare FTSO v2 预言机锚定报价, 本版改用 BSC 链上池子价格作为
"链上真值"锚, 并保留原策略的四大核心机制:

  1. 链上锚定定价    — 用 DEX 池子 slot0 价格, 不迷信 CEX 中间价
  2. 背离溢价        — |CEX − DEX| 超过阈值时线性扩 spread(原 FTSO 背离逻辑)
  3. 库存偏斜 skew   — 按目标库存比例(默认 50/50)双边调价, 均值回归
  4. ATR 波动率 sizing — spread = max(tick_size, k × ATR)
  5. 硬 kill-switch  — 数据陈旧 / 回撤超限立即停机(沿用 silent-martin 的安全设计)

数据流:
  - DEX 价格: Uniswap V3 / PancakeSwap V3 池子 slot0()(链上)
  - CEX 价格: Gate.io ticker(可选, 失败则降级为纯链上模式)
  - 波动率:   Gate.io K 线算 ATR(可选), 不可用时用配置的自适应波动率
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

import httpx

from base_agent import (
    CATEGORY_GRID_TRADING,
    AgentConfig,
    BaseAgent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BSC 地址
# ---------------------------------------------------------------------------

V3_POSITION_MANAGER = "0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613"

def _web3():
    """惰性导入 web3。

    web3 只用于 to_checksum_address(纯 RPC 路径), 但顶层 import 会让整个
    模块在 web3 缺失或依赖链损坏时不可导入 —— 连 tick 对齐、网格报价这些
    纯算术逻辑都测不了。宁可推迟到真正发起 RPC 时才报错。
    """
    try:
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError(
            "web3 不可用, 无法发起链上调用(报价计算本身不需要 web3)"
        ) from exc
    return Web3


WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT = "0x55d398326f99059fF775485246999027B3197955"
USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

BSC_RPCS = ["https://bsc.publicnode.com", "https://1rpc.io/bnb"]

GATE_TICKER_URL = "https://api.gateio.ws/api/v4/spot/tickers"
GATE_KLINE_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"

PM_ABI = [
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"name": "", "type": "address"}],
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
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
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

TICK_BASE = 1.0001


@dataclass
class GridConfig(AgentConfig):
    """Grid / 做市 agent 专属参数"""

    token0: str = WBNB
    token1: str = USDT
    quote_token: str = USDT      # 计价货币, 用于自动纠正价格方向
    pool_fee: int = 500          # V3 fee tier
    pool_address: str = ""       # 直接给池子地址(优先于 token/fee 组合)

    # ---- silent-martin 核心参数 ----
    spread_base_bps: float = 15.0      # 基础半价差(bps)
    dislocation_k: float = 2.0         # 背离时 spread 放大倍数
    dislocation_threshold_bps: float = 30.0   # 背离阈值(bps)
    target_ratio: float = 0.50         # 目标库存比例
    skew_aggression: float = 0.3       # 偏斜调整速度
    atr_lookback: int = 14             # ATR 窗口
    atr_multiplier: float = 2.0        # spread = k × ATR
    grid_levels: int = 3               # 单边网格层数

    # ---- 仓位 ----
    max_inventory: float = 10_000.0
    max_order_size: float = 500.0
    current_inventory: float = 5_000.0
    current_cash: float = 5_000.0

    # ---- 数据源 ----
    gate_pair: str = "BNB_USDT"        # CEX 对照(可选)
    use_cex: bool = True
    rpc_url: str = ""
    rpc_throttle_sec: float = 0.3


def tick_to_price(tick: int) -> float:
    return TICK_BASE ** tick


# 偏斜最多只能吃掉半价差的这个比例。skew 的作用是把双边整体平移以实现均值
# 回归, 但它绝不能大到让 ask 跌破 fair(或 bid 涨过 fair) —— 那等于主动
# 贱卖/高价接盘, 是做市最不该犯的错。留 10% 保证双边永远不交叉。
MAX_SKEW_OF_HALF_SPREAD = 0.9


def clamp_skew(skew: float, half_spread: float) -> float:
    """把库存偏斜限制在半价差之内, 保证 bid < fair < ask 恒成立。

    skew 是相对量(如 0.075 表示平移 fair 的 7.5%), 被平均分配到双边
    (各 skew/2)。只要 |skew/2| < half_spread, 双边就不可能交叉。
    """
    if half_spread <= 0:
        return 0.0
    limit = half_spread * MAX_SKEW_OF_HALF_SPREAD * 2
    return max(-limit, min(limit, skew))


def level_sizes(max_order_size: float, grid_levels: int) -> list[float]:
    """把 max_order_size 按 1/lvl 权重分摊到各档, 保证单边合计恰好等于上限。

    原实现 size = max_order_size / lvl, 三档合计 1.83 倍上限 —— 配置名叫
    "max_order_size" 却约束不住, 实际敞口比配置值大近一倍。
    """
    if grid_levels <= 0 or max_order_size <= 0:
        return []
    weights = [1.0 / lvl for lvl in range(1, grid_levels + 1)]
    total = sum(weights)
    return [max_order_size * w / total for w in weights]


def sqrt_price_x96_to_price(sqrt_price_x96: int, decimals0: int = 18, decimals1: int = 18) -> float:
    """
    Uniswap V3: price = (sqrtPriceX96 / 2^96)^2, 需按 decimals 差校正。

    token1 价格(以 token0 计价) = (sqrtPriceX96^2 / 2^192) × 10^(decimals0 - decimals1)
    """
    ratio = (sqrt_price_x96 / (2**96)) ** 2
    return ratio * (10 ** (decimals0 - decimals1))


class GridTradingAgent(BaseAgent):
    """silent-martin BSC 移植版 — 网格做市"""

    CATEGORY = CATEGORY_GRID_TRADING

    def __init__(self, config: GridConfig | None = None):
        super().__init__(config or GridConfig())
        self.config: GridConfig

        from erc8004 import make_web3, pick_rpc

        self.rpc_url = self.config.rpc_url or pick_rpc(BSC_RPCS)
        self.w3 = make_web3(self.rpc_url, timeout=25)

        self.pool_address = self._resolve_pool()
        self.pool = self.w3.eth.contract(
            address=_web3().to_checksum_address(self.pool_address), abi=POOL_ABI
        )

        # 价格方向校正: V3 池子里 token0/token1 按地址字典序排列,
        # slot0 返回的 price 恒为 "token1 per token0"。若计价币是 token0,
        # 必须取倒数, 否则价格会变成 1/691 这种量级, 背离检测直接爆表。
        self.pool_token0 = self.pool.functions.token0().call()
        self.pool_token1 = self.pool.functions.token1().call()
        quote_cs = _web3().to_checksum_address(self.config.quote_token)
        self._invert = quote_cs != _web3().to_checksum_address(self.pool_token1)

    def _resolve_pool(self) -> str:
        """解析池子地址: 优先用配置, 否则从 factory 动态查"""
        if self.config.pool_address:
            return _web3().to_checksum_address(self.config.pool_address)

        pm = self.w3.eth.contract(
            address=_web3().to_checksum_address(V3_POSITION_MANAGER), abi=PM_ABI
        )
        factory_addr = pm.functions.factory().call()
        factory = self.w3.eth.contract(
            address=_web3().to_checksum_address(factory_addr), abi=FACTORY_ABI
        )
        pool = factory.functions.getPool(
            _web3().to_checksum_address(self.config.token0),
            _web3().to_checksum_address(self.config.token1),
            self.config.pool_fee,
        ).call()

        if pool == "0x" + "0" * 40:
            raise ValueError(
                f"no pool for {self.config.token0}/{self.config.token1} fee={self.config.pool_fee}"
            )
        return _web3().to_checksum_address(pool)

    # ------------------------------------------------------------------
    # 数据层
    # ------------------------------------------------------------------

    def fetch_market_data(self) -> dict:
        data: dict = {"timestamp": time.time()}

        # 1) 链上 DEX 价格(主锚)
        slot0 = self.pool.functions.slot0().call()
        sqrt_price_x96 = slot0[0]
        dex_tick = slot0[1]
        # 主流 BSC 交易对多为 18/18 decimals
        raw_price = sqrt_price_x96_to_price(sqrt_price_x96, 18, 18)
        dex_price = (1.0 / raw_price) if self._invert else raw_price

        data["dex_price"] = dex_price
        data["dex_raw_price"] = raw_price
        data["inverted"] = self._invert
        data["dex_tick"] = dex_tick
        data["pool"] = self.pool_address

        # 2) CEX 价格(可选, 失败降级)
        cex_price = None
        if self.config.use_cex:
            try:
                resp = httpx.get(
                    GATE_TICKER_URL,
                    params={"currency_pair": self.config.gate_pair},
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    arr = resp.json()
                    if arr:
                        cex_price = float(arr[0].get("last", 0)) or None
            except Exception as exc:
                logger.debug("CEX price unavailable: %s", exc)
        data["cex_price"] = cex_price

        # 3) ATR 波动率(可选)
        atr_pct = None
        if self.config.use_cex:
            atr_pct = self._fetch_atr_pct()
        data["atr_pct"] = atr_pct

        return data

    def _fetch_atr_pct(self) -> float | None:
        """从 Gate.io K 线算 ATR 百分比(True Range 均值 / 收盘价)"""
        try:
            resp = httpx.get(
                GATE_KLINE_URL,
                params={
                    "currency_pair": self.config.gate_pair,
                    "interval": "15m",
                    "limit": self.config.atr_lookback + 1,
                },
                timeout=10.0,
            )
            if resp.status_code != 200:
                return None
            candles = resp.json()
            if len(candles) < 3:
                return None

            true_ranges = []
            last_close = None
            for c in candles:
                # Gate.io 格式: [timestamp, volume, close, high, low, open, ...]
                close = float(c[2])
                high = float(c[3])
                low = float(c[4])
                if last_close is not None:
                    tr = max(high - low, abs(high - last_close), abs(low - last_close))
                    true_ranges.append(tr)
                last_close = close

            if not true_ranges:
                return None
            atr = sum(true_ranges) / len(true_ranges)
            return (atr / last_close) * 100.0 if last_close else None
        except Exception as exc:
            logger.debug("ATR unavailable: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 策略核心 (silent-martin 四大机制)
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict:
        data = self._current_data
        dex_price = data.get("dex_price")
        if not dex_price:
            return {
                "metrics": {},
                "actions": [],
                "notes": "no dex price",
            }

        cex_price = data.get("cex_price")
        atr_pct = data.get("atr_pct")

        # ---- 1) 锚定: 以链上 DEX 价格为 fair ----
        fair = dex_price

        # ---- 2) 背离检测 ----
        dislocation_bps = 0.0
        if cex_price:
            dislocation_bps = abs(cex_price - dex_price) / dex_price * 10_000

        # ---- 3) spread = max(基础, k × ATR) + 背离溢价 ----
        atr_bps = (atr_pct * 100) if atr_pct else 0.0   # 百分比 -> bps
        spread_bps = max(self.config.spread_base_bps, self.config.atr_multiplier * atr_bps)

        if dislocation_bps > self.config.dislocation_threshold_bps:
            excess = dislocation_bps - self.config.dislocation_threshold_bps
            spread_bps += self.config.dislocation_k * excess

        spread_bps = min(spread_bps, 500.0)   # 上限 5%, 防止极端行情报出离谱价

        # ---- 4) 库存偏斜 (均值回归) ----
        total = self.config.current_inventory + self.config.current_cash
        inv_ratio = self.config.current_inventory / total if total else 0.5
        raw_skew = (self.config.target_ratio - inv_ratio) * self.config.skew_aggression

        # ---- 5) 生成双边网格报价 ----
        half_spread = spread_bps / 10_000 / 2

        # 夹紧必须在知道 half_spread 之后: 库存偏斜不能大到把双边推过公允价。
        skew = clamp_skew(raw_skew, half_spread)
        skew_clamped = abs(skew - raw_skew) > 1e-12
        skew_bps = skew * 10_000

        bid_center = fair * (1 - half_spread + skew / 2)
        ask_center = fair * (1 + half_spread + skew / 2)

        # 库存超过硬顶时只挂卖单, 单边去库存。max_inventory 曾经是个死参数
        # (定义了但从未被引用), 等于库存无上限。
        over_inventory = self.config.current_inventory > self.config.max_inventory
        if over_inventory:
            sides = ("SELL",)
        else:
            sides = ("BUY", "SELL")

        orders = []
        skipped_zero_price = 0
        sizes = level_sizes(self.config.max_order_size, self.config.grid_levels)
        for lvl in range(1, self.config.grid_levels + 1):
            step = half_spread * lvl
            size = sizes[lvl - 1]   # 越远层越小, 且单边合计恰为 max_order_size
            for side in sides:
                raw_price = (
                    bid_center * (1 - step) if side == "BUY" else ask_center * (1 + step)
                )
                price = round(raw_price, 6)

                # 小币(如 fair=4e-7)会被 round(x, 6) 压成 0.0, 而报价 0 意味着
                # 「任何价格都接受」—— 挂上去就是白送。宁可不挂。
                if price <= 0:
                    skipped_zero_price += 1
                    continue

                orders.append(
                    {
                        "side": side,
                        "level": lvl,
                        "price": price,
                        "size": round(size, 4),
                    }
                )

        # ---- 6) kill-switch 判定 ----
        if dislocation_bps > self.config.dislocation_threshold_bps * 5:
            return {
                "metrics": {
                    "fair_price": fair,
                    "dislocation_bps": round(dislocation_bps, 2),
                    "spread_bps": round(spread_bps, 2),
                },
                "actions": [],
                "notes": f"kill-switch: extreme dislocation {dislocation_bps:.0f}bps - halt quoting",
            }

        metrics = {
            "fair_price": round(fair, 6),
            "dex_price": round(dex_price, 6),
            "cex_price": round(cex_price, 6) if cex_price else None,
            "dislocation_bps": round(dislocation_bps, 2),
            "atr_pct": round(atr_pct, 4) if atr_pct else None,
            "spread_bps": round(spread_bps, 2),
            "inventory_ratio": round(inv_ratio, 4),
            "target_ratio": self.config.target_ratio,
            "skew_bps": round(skew_bps, 2),
            # 夹紧被触发说明库存失衡已超出价差能吸收的范围, 需要人工介入 ——
            # 静默夹紧会掩盖真实风险, 所以显式上报。
            "skew_clamped": skew_clamped,
            "over_inventory": over_inventory,
            "skipped_zero_price": skipped_zero_price,
            "bid": round(bid_center, 6),
            "ask": round(ask_center, 6),
            "pool": self.pool_address,
        }

        return {
            "metrics": metrics,
            "actions": [
                {
                    "type": "QUOTE",
                    "orders": orders,
                    "dry_run": self.config.dry_run,
                }
            ],
            "notes": f"quoting {len(orders)} grid orders, spread {spread_bps:.1f}bps",
            "orders": orders,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = GridConfig(
        agent_name="silent-martin.agent",
        agent_description=(
            "BSC port of silent-martin, a Hummingbot Botcamp CERTIFIED market-making "
            "strategy. Anchors every quote to the on-chain DEX pool price instead of CEX "
            "mid, widens spread on CEV/DEX dislocation, applies inventory skew toward a "
            "target ratio, sizes spread by ATR volatility, and halts on stale data or "
            "extreme dislocation via hard kill-switches."
        ),
        dry_run=True,
        network="mainnet",
        cycle_interval_sec=0,
    )

    agent = GridTradingAgent(cfg)
    agent.run(cycles=2)

    print("\n" + "=" * 70)
    print("FINAL STATUS")
    print("=" * 70)
    print(json.dumps(agent.current_status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
