"""
RebalanceKeeper — Configuration constants for Aave V3 on Sepolia testnet.

All on-chain addresses are from the official Aave address book
(https://github.com/aave/aave-address-book) for Sepolia testnet.
"""

import os
from dataclasses import dataclass, field
from typing import Dict


# ── KeeperHub MCP ──────────────────────────────────────────────
KEEPERHUB_MCP_URL = os.getenv("KEEPERHUB_MCP_URL", "https://app.keeperhub.com/mcp")
KEEPERHUB_API_KEY = os.getenv("KEEPERHUB_API_KEY", "")

# ── Wallet (Turnkey non-custodial, provisioned via KeeperHub) ──
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x1573C3d151200922375bC48012BB1f232B2cF531")
WALLET_INTEGRATION_ID = os.getenv("WALLET_INTEGRATION_ID", "flx4bwzye6tb6re68wost")

# ── Chain ──────────────────────────────────────────────────────
# Sepolia testnet (chainId = 11155111).  Switch to "1" for mainnet.
CHAIN_ID = os.getenv("CHAIN_ID", "11155111")
CHAIN_NAME = "Ethereum Sepolia"

# ── Arc Testnet (Circle stablecoin-native L1) ──
# Arc is an EVM-compatible L1. USDC is its native gas token.
# Docs: https://docs.arc.io  |  Explorer: https://testnet.arcscan.app
# NOTE: Sepolia testnet assets CANNOT be bridged to Arc — you must request
# Arc Testnet USDC from the Circle Faucet (https://faucet.circle.com).
ARC_RPC_URL = os.getenv("ARC_RPC_URL", "https://rpc.testnet.arc.network")
ARC_CHAIN_ID = int(os.getenv("ARC_CHAIN_ID", "5042002"))
ARC_CHAIN_NAME = "Arc Testnet"
ARC_EXPLORER = "https://testnet.arcscan.app"
# USDC ERC-20 interface (per Arc docs, read balances via this, 6 decimals).
# The native gas balance uses 18 decimals; we standardise on the ERC-20 6-decimal view.
ARC_USDC_ERC20 = "0x3600000000000000000000000000000000000000"
ARC_USDC_DECIMALS = 6
# Wallet to monitor on Arc. Set ARC_WALLET_ADDRESS in .env after creating one
# via the Circle Faucet. Falls back to the Sepolia WALLET_ADDRESS for local testing.
ARC_WALLET_ADDRESS = os.getenv("ARC_WALLET_ADDRESS", WALLET_ADDRESS)
# Reserve wallet the agent pulls USDC from / sweeps excess to.
# Set ARC_RESERVE_ADDRESS in .env (a second Arc Testnet address from the faucet).
ARC_RESERVE_ADDRESS = os.getenv("ARC_RESERVE_ADDRESS", "")
# Private keys (hex, 0x-prefixed). NEVER commit — .env is git-ignored.
# Operational wallet key signs sweep/outbound transfers; reserve key signs top-ups.
ARC_PRIVATE_KEY = os.getenv("ARC_PRIVATE_KEY", "")
ARC_RESERVE_PRIVATE_KEY = os.getenv("ARC_RESERVE_PRIVATE_KEY", "")

# ── Aave V3 Core Contracts (Sepolia) ──────────────────────────
AAVE_POOL = "0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951"
AAVE_POOL_ADDRESSES_PROVIDER = "0x012bAC54348C0E635dCAc9D5FB99f06F24136C9A"
AAVE_POOL_CONFIGURATOR = "0x7Ee60D184C24Ef7AfC1Ec7Be59A0f448A0abd138"
AAVE_ORACLE = "0x2da88497588bf89281816106C7259e31AF45a663"
AAVE_ACL_MANAGER = "0x7F2bE3b178deeFF716CD6Ff03Ef79A1dFf360ddD"
WETH_GATEWAY = "0x387d311e47e80b498169e6fb51d3193167d89F7D"

# ── Token Addresses (Sepolia) ─────────────────────────────────
# Collateral asset: WETH.  Debt asset: USDC.
TOKENS: Dict[str, Dict] = {
    "WETH": {
        "address": "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c",
        "aToken": "0x5b071b590a59395fE4025A0Ccc1FcC931AAc1830",
        "decimals": 18,
        "role": "collateral",
    },
    "USDC": {
        "address": "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8",
        "aToken": "0x16dA4541aD1807f4443d92D26044C1147406EB80",
        "decimals": 6,
        "role": "debt",
    },
    "DAI": {
        "address": "0xFF34B3d4Aee8ddCd6F9AFFFB6Fe49bD371b8a357",
        "aToken": "0x29598b72eb5CeBd806C5dCD549490FdA35B13cD8",
        "decimals": 18,
        "role": "debt",
    },
    "LINK": {
        "address": "0xf8Fb3713D459D7C1018BD0A49D19b4C44290EBE5",
        "aToken": "0x3FfAf50D4F4E96eB78f2407c090b72e86eCaed24",
        "decimals": 18,
        "role": "collateral",
    },
    "AAVE": {
        "address": "0x88541670E55cC00bEEFD87eB59EDd1b7C511AC9a",
        "aToken": "0x6b8558764d3b7572136F17174Cb9aB1DDc7E1259",
        "decimals": 18,
        "role": "collateral",
    },
    "WBTC": {
        "address": "0x29f2D40B0605204364af54EC677bD022dA425d03",
        "aToken": "0x1804Bf30507dc2EB3bDEbbbdd859991EAeF6EefF",
        "decimals": 8,
        "role": "collateral",
    },
    "USDT": {
        "address": "0xaA8E23Fb1079EA71e0a56F48a2aA51851D8433D0",
        "decimals": 6,
        "role": "debt",
    },
    "GHO": {
        "address": "0xc4bF5CbDaBE595361438F8c6a187bDc330539c60",
        "decimals": 18,
        "role": "debt",
    },
}

# Convenience accessors
COLLATERAL_TOKEN = "WETH"   # supply this as collateral
DEBT_TOKEN = "USDC"          # borrow this against collateral


def token_addr(symbol: str) -> str:
    return TOKENS[symbol]["address"]


def token_decimals(symbol: str) -> int:
    return TOKENS[symbol]["decimals"]


# ── Rebalancer Parameters ─────────────────────────────────────
@dataclass
class RebalanceConfig:
    """Thresholds and behaviour for the rebalance loop.

    Health-factor zones:
      SAFE     HF >= safe_threshold        → no action, just log
      WARNING  warn_threshold <= HF < safe → start watching, log trend
      DANGER   danger_threshold <= HF < warn → active rebalance (repay/supply)
      CRITICAL HF < danger_threshold        → emergency repay max
    """

    # ── Health factor zones ──
    safe_threshold: float = 2.0       # above this = healthy
    warn_threshold: float = 1.5       # start watching / light rebalance
    danger_threshold: float = 1.2     # active rebalance, larger amounts
    # below danger_threshold = critical → emergency mode

    # ── Trend analysis ──
    trend_window: int = 5             # number of readings for trend
    trend_decline_rate: float = 0.02  # HF drop per reading → pre-emptive trigger

    # ── Strategy parameters ──
    # Repay: fraction of total debt to repay per trigger
    repay_fraction_warn: float = 0.10   # 10% at warning level
    repay_fraction_danger: float = 0.25  # 25% at danger level
    repay_fraction_critical: float = 0.50  # 50% at critical level

    # Supply: extra collateral to add
    supply_boost_amount: str = "0.01"   # 0.01 WETH

    # Interest rate arbitrage: switch debt if APY diff > threshold
    arb_apy_threshold: float = 3.0     # percentage points

    # Polling interval in seconds
    monitor_interval: int = 30

    # Max retries on a failed execution
    max_retries: int = 3

    # Idempotency key prefix (appended with timestamp)
    idempotency_prefix: str = "rbk"

    # Audit log file path
    audit_log_path: str = "logs/audit.jsonl"

    # Health-factor history for dashboard (in-memory ring buffer)
    history_size: int = 500

    # Interest rate mode for borrow/repay (2 = variable, 1 = stable)
    interest_rate_mode: str = "2"

    # Cooldown: minimum seconds between rebalance actions
    cooldown_seconds: int = 120

    # SECURITY: hard ceiling (USD) on a single rebalance action. A sane upper
    # bound protects against a bad HF reading or a units bug producing a runaway
    # repay/supply. Overridable via MAX_REBALANCE_USD env var.
    max_rebalance_usd: float = float(os.getenv("MAX_REBALANCE_USD", "10000"))


# Default config instance
REBALANCE_CONFIG = RebalanceConfig()


# ── Arc (Treasury) Rebalance Config ───────────────────────────
@dataclass
class ArcRebalanceConfig:
    """Treasury-health model for ArcKeeper on Arc.

    On Arc there is no Aave-style lending pool (yet), so ArcKeeper manages a
    USDC *treasury*: it monitors its operating USDC balance and, when it drops
    below a floor (e.g. after autonomous payments / nanopayments), it
    rebalances by pulling USDC back from a reserve wallet to restore the floor.
    This maps directly to the Agentic Economy track:
      "agents that manage treasury, settle jobs, rebalance funds using USDC".

    Treasury Health = current_usdc / floor_usdc
      SAFE     health >= safe
      WARNING  warn <= health < safe      → watch
      DANGER   danger <= health < warn    → rebalance (top-up)
      CRITICAL health < danger            → emergency max top-up
    """

    floor_usdc: float = 50.0          # minimum operating USDC balance (operational wallet holds 80 USDC → reads SAFE; reserve holds 40 USDC)
    ceiling_usdc: float = 75.0        # max operating USDC to keep; excess above this is swept to reserve (agent holds only `ceiling` in ops)
    sweep_fraction: float = 1.0       # fraction of the excess (current - ceiling) swept per trigger
    safe_threshold: float = 2.0
    warn_threshold: float = 1.5
    danger_threshold: float = 1.2

    # Fraction of the deficit (floor - current) to pull back on each trigger
    topup_fraction_warn: float = 0.50
    topup_fraction_danger: float = 1.0     # fully restore floor
    topup_fraction_critical: float = 1.0

    monitor_interval: int = 30


# Default Arc config instance
ARC_REBALANCE_CONFIG = ArcRebalanceConfig()
