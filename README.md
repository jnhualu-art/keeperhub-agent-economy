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

Three independent guarantees, none of which depends on trusting this codebase:

1. **Non-custodial signing** — the agent holds only an API key. Signing happens inside KeeperHub's Turnkey wallet. This code cannot exfiltrate a private key because it never has one.
2. **Fail-closed execution** — `dry_run` defaults to `true`. With no API key or no wallet configured, the fleet refuses to broadcast anything. Every execution carries an `idempotency_key`, so a retry after a network failure cannot double-spend.
3. **Independent verification** — KeeperHub's execution report is not taken at face value. Every claim it makes is re-checked against a third-party node the agent does not control. See [below](#independent-verification).

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

## Independent verification

Everything above rests on KeeperHub telling us what it did. That is self-reporting, and self-reporting is not evidence.

`logs/audit.jsonl` records what KeeperHub **returned to us**. If it misreported an amount, or reported success on a transaction that actually reverted, the audit log would be wrong and nothing in this repo could tell. So a third layer exists: for every transaction KeeperHub claims to have executed, an **unrelated public node** is asked what actually happened, and the two are reconciled field by field.

```bash
python scripts/run_reconcile.py
```

```
[OK  ] 0x5c32bc4c9094…  PROTECT    13.23 -> 13.230000 USDC  (Repay->Repay)
  + receipt_available: found on independent node
  + tx_success: status=1
  + execution_topology: from=0x809d8252aa… to=0x5af5194b4b… — relayed, gas sponsored
  + event_emitted: Pool emitted Repay
  + event_matches_action: PROTECT -> Repay
  + wallet_matches: event subject = 0x1573c3d151…
  + amount_matches: claimed 13.23 vs on-chain 13.230000 (delta 0.000000 <= 0.1323)

2/2 claims independently verified
```

Full output: [`docs/reconciliation-2026-09-03.txt`](docs/reconciliation-2026-09-03.txt). Exit code is non-zero on any discrepancy, so it drops straight into cron or CI.

**A discovery the reconciler made immediately.** KeeperHub executes through a relayer — the on-chain `from` is `0x809d8252…` (the relayer EOA) and `to` is `0x5af5194b…` (KeeperHub's router), not our wallet hitting Aave directly. That is what gas sponsorship looks like on-chain, and it means the obvious check (`from == our_wallet`) fails on every single legitimate transaction. Ownership has to be proven from the `user` / `onBehalfOf` field inside the Pool event, not from transaction fields. The first version got this wrong and flagged both real transactions as mismatches.

**Two directions, one fact set.**

| Path | Direction | Needs infrastructure |
|---|---|---|
| `src/reconciler.py` | Pull — ask a third-party node what happened | Nothing. Runs today |
| [`oz-monitor/`](oz-monitor/) + `src/alert_receiver.py` | Push — an [OpenZeppelin Monitor](https://github.com/openzeppelin/openzeppelin-monitor) instance watches the Pool and signs a webhook to us | A self-hosted Monitor |

The push path's configuration is checked in and ready to drop into a self-hosted Monitor — network, monitor, trigger and a Python filter that discards events belonging to other people's positions. The receiver verifies the `X-Signature` HMAC (`HMAC-SHA256(secret, payload+timestamp)`) and rejects replays outside a 5-minute window.

> **Defender is gone.** OpenZeppelin disabled new Defender sign-ups on 2025-06-30 and retired the hosted platform on 2026-07-01. The only path now is the open-source, self-hosted Monitor (AGPL v3) — which is what this targets. See [`oz-monitor/README.md`](oz-monitor/README.md).

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

## Security notes

An agent that claims to protect funds has to survive being audited itself, so
the executor layer was reviewed against a hostile-upstream threat model:
*what happens when a decision agent emits a malformed, contradictory, or
outright oversized action?* Every finding below was real, reproduced with a
script rather than reasoned about, and fixed. `scripts/audit_probe.py` reruns
the whole set and prints pass/fail per item.

| Finding | Why it mattered | Status |
|---|---|---|
| Hard cap bypassable | The cap was checked against `amount_usd`, which defaults to `0.0`. An upstream action carrying only `amount_base` sailed straight through — a probe put ~1B USDC of base units on the wire. | Fixed: amounts are normalised from `amount_base`, and `amount_usd`/`amount_base` are cross-checked against each other |
| Idempotency key was a timestamp | `f"protect-{int(time.time())}"` produces a *different* key for a retry one second later, so KeeperHub treats it as a new transaction. The docstring claimed protection that did not exist. | Fixed: key is derived from action content plus a time bucket |
| Floating-point amount conversion | `int(13.23 * 10**6)` is `13229999`, not `13230000` — IEEE754 rounding, and it silently truncates. About **1.2%** of amounts in 0.01–2000.00 USD lose 1 base unit. | Fixed: `Decimal` throughout |
| Audit written after broadcast | If the process died between sending the transaction and writing the audit line, the chain would hold a transaction the reconciler could never see. | Fixed: write-ahead intent record; when the audit log is unwritable, **zero transactions are sent** |
| No liquidation floor | The executor would forward a borrow whose declared post-trade health factor was below 1.0. | Fixed: `hf_after` below 1.0 is refused |
| Unbounded session retry | A persistently invalid MCP session recursed until `RecursionError`. | Fixed: one retry, then error |

Two things that were checked and are **not** problems, recorded so they don't
get re-litigated: no private keys or secrets are in the repository or its
history (`.env` has never been committed), and the kill-switch genuinely
prevents actions from reaching the executor — a probe confirmed that a halting
agent contributes zero actions.

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
│   ├── env.py                 # shared .env loader (keeps secrets out of modules)
│   ├── reconciler.py          # pull path: audit log vs third-party node
│   ├── evm.py                 # minimal JSON-RPC client + Aave event decoding
│   ├── alert_receiver.py      # push path: signed webhook ingestion
│   ├── rebalancing_agent.py   # PancakeSwap V3 LP rebalancing
│   ├── yield_agent.py         # BSC yield routing
│   ├── grid_agent.py          # BSC grid market-making
│   └── main.py                # fleet orchestration entrypoint
├── oz-monitor/                # ready-to-use OpenZeppelin Monitor config
│   ├── networks/sepolia.json
│   ├── monitors/aave_v3_keeperhub_execution.json
│   ├── triggers/keeperhub_webhook.json
│   └── filters/keeperhub_execution_filter.py   # `--selftest` runs standalone
├── scripts/
│   ├── setup_aave_position_web3.py   # seed a test position to reproduce the demo
│   ├── test_keeperhub_connection.py  # MCP connectivity smoke test
│   ├── run_live_borrow.py            # live path 2 (borrow); `--dry` plans only
│   ├── run_reconcile.py              # independent reconciliation; exit≠0 on drift
│   └── probe_*.py                    # on-chain diagnostics (tools, caps, reserves)
├── docs/reconciliation-2026-09-03.txt   # captured reconciliation output
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

# 4) verify — no API key needed; asks an unrelated node what really happened
.\.venv\Scripts\python scripts/run_reconcile.py
```

Step 4 needs no KeeperHub credentials at all. That is the point: it is the check that does not depend on KeeperHub's cooperation. Set `SEPOLIA_RPC_URL` to use your own node; otherwise it falls back through a list of public Sepolia endpoints.

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

The safety guarantees above are enforced by a test suite (181 tests, no network access — every on-chain interaction is faked):

```bash
pip install pytest
python -m pytest
```

No flags needed: `pytest.ini` pins collection to `tests/` and disables the `xonsh` plugin, which otherwise crashes the session in any non-terminal environment (CI, background execution).

Coverage highlights:

| Suite | What it pins down |
|---|---|
| `test_reconciler.py` | the trust layer: a real Repay log reconciling clean, revert reported as `REVERTED`, missing tx as `NOT_FOUND`, RPC failure surfaced not swallowed, wallet/event/amount mismatches each failing independently, and relayed topology recorded without being punished |
| `test_evm.py` | ABI decoding against **real Sepolia logs** — including the trap where `Borrow`'s non-indexed `user` sits before `amount`, so the amount is the second data word; multi-endpoint fallback; user-agent requirement |
| `test_alert_receiver.py` | HMAC signature accepted, wrong secret / tampered payload / stale timestamp rejected, replay window, non-object payload rejected, and a rejected webhook writing nothing to disk |
| `test_executor.py` | dry-run plan unit conversion (USDC 6-decimals, `interestRateMode=2`), fail-closed fallback when no API key, live path emits tx hash, MCP errors recorded not raised, JSONL audit trail, `REBALANCE` venue dispatch, executor-side hard cap blocking oversized borrows |
| `test_capital_efficiency_agent.py` | the risk model that sizes every borrow: `max_debt = collateral × threshold / HF_target`, safety discount, on-chain cap clamping, negative-headroom clamping, graceful degradation on MCP error / malformed data, and the hand-off boundary where the agent defers to HealthFactor |
| `test_executor_safety.py` | the "executor never trusts upstream" invariants: hard cap cannot be bypassed by omitting `amount_usd`, a borrow declaring `hf_after < 1.0` is refused, contradictory `amount_usd`/`amount_base` is rejected, idempotency keys are content-derived, and **write-ahead auditing means zero transactions are sent when the audit log is unwritable** |
| `test_test_isolation.py` | meta-tests: the suite cannot see real credentials, and `KeeperHubClient`'s bound default `api_key` is empty. Guards against a bug where `scripts/test_keeperhub_connection.py` matched pytest's `test_*.py` pattern, got imported during collection, and loaded `.env` before `src.config` snapshotted the real API key — so single-file runs passed while full runs failed *and ran holding live credentials* |
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
