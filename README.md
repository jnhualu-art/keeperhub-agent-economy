# KeeperHub as a Conditional Execution Layer for DeFi Position Management

> **Hackathon**: KeeperHub — The Agent Economy Hackathon (DoraHacks, Season 2)
> **Track**: Best Integration
> **Demo video**: https://youtu.be/C48UpGSQJLQ
> **Live proof**: two sponsored transactions on Aave V3 (Sepolia), see [below](#live-on-chain-proof)

---

## The use case

KeeperHub is usually described as *"execute and sponsor transactions from an agent."* This project asks a different question:

> **What if KeeperHub were the execution backend for agents that watch a position and act only when a condition breaks?**

That turns KeeperHub from a transaction relay into a **conditional execution layer** — the missing half of any automated DeFi risk system. You can read chain state with an RPC for free; you cannot *react* on-chain without a signing path. KeeperHub is that path, and it removes the two things that normally block it: **custody** (Turnkey signs, your agent never holds a key) and **gas** (transactions are sponsored, the monitored wallet needs zero ETH).

The concrete use case demonstrated here: **a lending position that defends itself from liquidation without its owner being online.**

```
Aave V3 health factor drops below threshold
      │
      ▼
HealthFactorAgent reads getUserAccountData()  ──►  classifies SAFE / WARN / DANGER / CRITICAL
      │                                             computes the exact repay amount
      ▼
Executor resolves action ──► KeeperHub MCP ──► Turnkey wallet signs
      │                                             (gas sponsored)
      ▼
aave-v3/repay broadcast on-chain  ──►  health factor recovers  ──►  audit.jsonl
```

This is the "self-healing position" pattern. It is not specific to Aave — swap the condition, and the same Executor + KeeperHub path serves LP rebalancing, yield migration, and grid market-making.

## Why this matters for KeeperHub's users

Three properties make this pattern a customer-acquisition story, not just a demo:

| Property | What it unlocks |
|---|---|
| **The monitored wallet needs no ETH** | A retail user can point a guardian agent at their existing Aave position and walk away. No funding step, no gas onboarding — this is what makes it viable for *many individuals and small businesses*, not just crypto-native power users. |
| **Zero custody** | An agent holding an API key can move funds, but the private key never leaves Turnkey's HSM. This is the objection every enterprise risk team raises first, and KeeperHub already answers it. |
| **Every action is auditable** | `logs/audit.jsonl` records each decision, each skip, and the reason. Compliance teams can replay the agent's entire decision history. |

## Trustlessness

Two independent guarantees, neither of which depends on trusting this codebase:

1. **Non-custodial signing** — the agent holds only an API key. Signing happens inside KeeperHub's Turnkey wallet. This code cannot exfiltrate a private key because it never has one.
2. **Fail-closed execution** — `dry_run` defaults to `true`. With no API key or no wallet configured, the fleet refuses to broadcast anything. Every execution carries an `idempotency_key`, so a retry after a network failure cannot double-spend.

The second one matters because a "guardian" that misfires is worse than no guardian. See [Safety](#safety-design).

## Live on-chain proof

This is not a plan-only demo. **Two different agents, two different DeFi actions, one shared execution layer** — both broadcast on Sepolia through KeeperHub and publicly verifiable.

**Monitored wallet**: `0x1573C3d151200922375bC48012BB1f232B2cF531`

### Path 1 — Defend: HealthFactorAgent repays debt when HF drops

`HealthFactorAgent → Executor → KeeperHub MCP → Aave V3 Sepolia`

| Stage | Action | Health factor | On-chain proof |
|---|---|---|---|
| ① Create the danger | Borrow 27 USDC through KeeperHub | 1.5668 → **1.2471** (WARN) | [`0x3985c67d…79587`](https://sepolia.etherscan.io/tx/0x3985c67d4068e3756f04378f7f72575e63d9fbbe6ea0bb82cf08e50f1ac79587) |
| ② Agent decides | Reads HF = 1.2471 → WARN → emits PROTECT (repay 13.23 USDC) | — | `logs/audit.jsonl` |
| ③ Self-heal | Executor routes `aave-v3/repay`, broadcast via KeeperHub | **1.2471 → 1.3856** (recovered) | [`0x5c32bc4c…759e9`](https://sepolia.etherscan.io/tx/0x5c32bc4c9094e96210ad2b1a4149310849c64429a1e5a003fd6192c7a8d759e9) |

### Path 2 — Attack: CapitalEfficiencyAgent frees idle borrowing power when HF is too high

`CapitalEfficiencyAgent → Executor → KeeperHub MCP → Aave V3 Sepolia`

The mirror-image problem is just as common: users over-collateralise for safety and leave borrowing power sitting idle, earning nothing. This position had **40.52 USD of unused borrowing power** parked at HF 1.38 — far above the 1.0 liquidation line.

| Stage | Action | Health factor | On-chain proof |
|---|---|---|---|
| ① Agent reads position | `aave-v3/get-user-account-data` → collateral 200.00 / debt 119.48 / available 40.52 | 1.3809 | `logs/audit.jsonl` |
| ② Agent sizes the move | Solves for the borrow that keeps HF ≥ 1.30, applies a 0.90 safety discount, caps at the on-chain borrow limit → **borrow 6.69 USDC** | projected **1.3077** | `logs/audit.jsonl` |
| ③ Broadcast | Executor routes `aave-v3/borrow` via KeeperHub | **1.3809 → 1.3077** (as predicted) | [`0x0a565f54…8897`](https://sepolia.etherscan.io/tx/0x0a565f54e189515e2fcab74afa28f70b19824ddfdc4ce685d1a974cf137b8897) |

The projected health factor (1.3077) matched the post-transaction on-chain value exactly, and the wallet's USDC balance moved 111.269811 → 117.959811.

**Why two paths matter more than one.** A single executed transaction proves KeeperHub can broadcast. Two agents driving *opposite* actions — one repaying to raise HF, one borrowing to lower it — through the same Executor, the same risk controls and the same audit pipeline is what makes this an execution **layer** rather than a one-off script.

All transactions are `sponsored: true`. Reproduce path 2 with `python scripts/run_live_borrow.py` (add `--dry` to plan without broadcasting).

## The fleet

Five decision agents share one execution layer and one audit pipeline. Each reads real on-chain state; each emits an action that the Executor resolves against KeeperHub.

| Agent | Reads | Decides | KeeperHub action |
|---|---|---|---|
| **HealthFactor** (`hfsentinel.agent`) | Aave V3 `getUserAccountData` | HF tiering (SAFE/WARN/DANGER/CRITICAL) + exact repay sizing | `aave-v3/repay` — **executed on-chain** ✅ |
| **CapitalEfficiency** (`capital-efficiency.agent`) | Aave V3 `getUserAccountData` | Solves for the borrow size that keeps HF above a hard floor, then applies a safety discount and the on-chain borrow cap | `aave-v3/borrow` — **executed on-chain** ✅ |
| **Rebalancing** (`rangeguard.agent`) | PancakeSwap V3 position ticks + pool `slot0` | Out-of-range / near-boundary detection, new range | `execute_contract_call` plan |
| **Yield** (`yieldpilot.agent`) | DefiLlama BSC pool APY / TVL | Risk-adjusted score, migrate only if uplift > 15% | `execute_contract_call` plan |
| **Grid** (`silent-martin.agent`) | BSC DEX price + CEX divergence + ATR | Chain-anchored quoting + inventory skew + kill-switch | DEX grid quoting plan |

**HealthFactor and CapitalEfficiency have executed on testnet.** The other three produce fully-formed, ready-to-fire call plans but run `dry_run: true` — they read live chain data and make real decisions, but do not broadcast. This is stated plainly rather than papered over.

HealthFactor and CapitalEfficiency are deliberately paired: they manage the *same* Aave position in opposite directions, and their responsibilities are bounded so they never fight each other. CapitalEfficiency only borrows while HF stays above 1.35, and hands off entirely below 1.30 — below that the position belongs to HealthFactor, whose job is to repay.

## Safety design

- `dry_run` defaults on — no API key or no wallet means **no broadcast, ever**
- Hard kill-switch: drawdown limits, stale-data detection, consecutive-failure halts
- `idempotency_key` on every execution, so retries cannot double-spend
- Full audit trail in `logs/audit.jsonl` (tx hash, or the reason for skipping)

## Repository layout

```
keeperhub-agent-economy/
├── src/
│   ├── base_agent.py          # shared agent base + ERC-8004 registration
│   ├── config.py              # Aave V3 / KeeperHub / token constants
│   ├── keeperhub_client.py    # KeeperHub MCP HTTP client (execution layer)
│   ├── executor.py            # integration core: action → KeeperHub routing + audit
│   ├── health_factor_agent.py # Aave V3 liquidation defense (live repay)
│   ├── capital_efficiency_agent.py  # idle-borrowing-power release (live borrow)
│   ├── env.py                # shared .env loader (keeps secrets out of modules)
│   ├── rebalancing_agent.py   # PancakeSwap V3 LP rebalancing
│   ├── yield_agent.py         # BSC yield routing
│   ├── grid_agent.py          # BSC grid market-making
│   └── main.py                # fleet orchestration entrypoint
├── scripts/
│   ├── setup_aave_position_web3.py   # seed a test position to reproduce the demo
│   ├── test_keeperhub_connection.py  # MCP connectivity smoke test
│   ├── run_live_borrow.py            # live path 2 (borrow); `--dry` plans only
│   └── probe_*.py                    # on-chain diagnostics (tools, caps, reserves)
├── logs/audit.jsonl           # execution audit trail (gitignored, reproducible)
├── .env.example
└── requirements.txt
```

## Running it

```bash
# 1) venv
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2) dry run — no API key needed, never broadcasts
set DRY_RUN=true
.\.venv\Scripts\python src/main.py

# 3) live — requires KeeperHub API key + provisioned wallet
#    copy .env.example -> .env, fill KEEPERHUB_API_KEY / MONITOR_ADDRESS
set DRY_RUN=false
.\.venv\Scripts\python src/main.py
```

Dry-run output:

```
KEEPERHUB AGENT ECONOMY — FLEET REPORT
dry_run            : True
agents             : 4 (4 ok)
total actions      : 3
executed on-chain  : 0
planned (audit)    : 3
skipped            : 0
[ HealthFactor]    ok  actions=1  HF 1.12 DANGER -> repay plan
[ Rebalancing]     ok  actions=1  ...
[      Yield]      ok  actions=1  ...
[      Grid]       ok  actions=1  ...
```

## Tests

The safety guarantees above are enforced by a test suite (77 tests, no network access — every on-chain interaction is faked):

```bash
pip install pytest
python -m pytest tests/ -v
```

Coverage highlights:

| Suite | What it pins down |
|---|---|
| `test_executor.py` | dry-run plan unit conversion (USDC 6-decimals, `interestRateMode=2`), fail-closed fallback when no API key, live path emits tx hash, MCP errors recorded not raised, JSONL audit trail, `REBALANCE` venue dispatch, executor-side hard cap blocking oversized borrows |
| `test_capital_efficiency_agent.py` | the risk model that sizes every borrow: `max_debt = collateral × threshold / HF_target`, safety discount, on-chain cap clamping, negative-headroom clamping, graceful degradation on MCP error / malformed data, and the hand-off boundary where the agent defers to HealthFactor |
| `test_base_agent.py` | all three kill-switches (drawdown / stale data / consecutive errors), halt semantics, ERC-8004 registration file shape |
| `test_health_factor_agent.py` | Aave base-unit normalization (incl. `2^256-1` → `inf` for no-debt), SAFE/WARN/CRITICAL thresholds, repay sizing per tier, plus a regression test replaying the real Sepolia incident (HF 1.2471 → repay 13.23 USDC) |
| `test_keeperhub_client.py` | amount→base-unit conversion, `interestRateMode` required-field regression, idempotency-key attempt suffixing, retry exhaustion, error normalization to `MCPError` |

## Submission status

- **Register as Hacker** — open until the 18 Sep deadline; registration and submission are separate actions
- **Submit BUIDL** — opens **6 Sep 12:00 CEST**
- **Deadline** — **18 Sep 12:00 CEST** (18:00 Beijing)
- Event page: https://dorahacks.io/hackathon/agent-economy/detail

> The **Best Feature bounty** is judged as *"a pull request to the KeeperHub repository — can we merge it and build on it?"*. It is therefore tracked separately from this repo and is not claimed by this submission.

## Links

```
Repo      : https://github.com/jnhualu-art/keeperhub-agent-economy
Demo      : https://youtu.be/C48UpGSQJLQ
Hackathon : https://dorahacks.io/hackathon/agent-economy/detail
Wallet    : 0x1573C3d151200922375bC48012BB1f232B2cF531
```

## Author

Solo — 陆俊华 ([@Jhhu73965779](https://x.com/Jhhu73965779))

MIT licensed.
