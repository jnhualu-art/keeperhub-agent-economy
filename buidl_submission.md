# KeeperHub 第二轮 · BUIDL 提交文案

> 用法：9/6 12:00 CEST 提交闸门开闸后，按下面内容复制到对应字段。
> 视频外链已填入：`https://youtu.be/C48UpGSQJLQ`（YouTube Unlisted）。
>
> ⚠️ **2026-09-02 修正**：原「BUIDL 2 · Bounty Best Feature」方案作废。
> 官方 bounty 评审标准是 **"Ship a feature as a pull request to the KeeperHub repository…
> Judged on whether we can merge it and build on it"** —— 交自己仓库里的模块不计分。
> 本文件现在**只保留主赛道一份 BUIDL**，bounty 走 PR 路线单独跟踪（见文末）。

---

## 🏆 主赛道 Best Integration（$4k）

**Track**：Best Integration

**Project Name**：
```
KeeperHub as a Conditional Execution Layer for DeFi Position Management
```

**Tagline / 一句话简介**：
```
把 KeeperHub 从"代发交易的中继"变成"条件式执行层"——一个会自己还债、防清算的 DeFi 仓位
```

**Long Description**：
```
KeeperHub is usually described as "execute and sponsor transactions from an agent."
This project asks a different question: what if KeeperHub were the execution backend
for agents that watch a position and act only when a condition breaks?

That turns KeeperHub from a transaction relay into a conditional execution layer —
the missing half of any automated DeFi risk system. You can read chain state with an
RPC for free; you cannot react on-chain without a signing path. KeeperHub is that
path, and it removes the two things that normally block it: custody (Turnkey signs,
the agent never holds a key) and gas (transactions are sponsored, so the monitored
wallet needs zero ETH).

The concrete use case: a lending position that defends itself from liquidation
without its owner being online.

  HF drops → HealthFactorAgent reads getUserAccountData() → classifies WARN
           → Executor resolves action → KeeperHub MCP → Turnkey signs
           → aave-v3/repay broadcast → HF recovers → audit.jsonl

This is not a plan-only demo. TWO different agents drove TWO opposite DeFi actions
on Aave V3 (Sepolia) — through the same Executor, the same risk controls and the
same audit pipeline:

  PATH 1 — DEFEND (HealthFactorAgent lowers risk):
  • Borrow 27 USDC via KeeperHub   → HF 1.5668 → 1.2471 (WARN)
  • Agent computes PROTECT: repay 13.23 USDC
  • Executor routes aave-v3/repay  → HF 1.2471 → 1.3856 (recovered)

  PATH 2 — ATTACK (CapitalEfficiencyAgent puts idle capital to work):
  The mirror-image problem: users over-collateralise for safety and leave borrowing
  power sitting idle, earning nothing. This position had 40.52 USD of unused
  borrowing power parked at HF 1.38, far above the 1.0 liquidation line.
  • Agent reads the position (collateral 200.00 / debt 119.48 / available 40.52)
  • Solves for the borrow size that keeps HF at or above a 1.30 floor, applies a
    0.90 safety discount, then clamps to the on-chain borrow limit → borrow 6.69 USDC
  • Executor routes aave-v3/borrow → HF 1.3809 → 1.3077, exactly as predicted

The projected health factor (1.3077) matched the post-transaction on-chain value
exactly, and the wallet's USDC balance moved 111.269811 → 117.959811.

Why two paths matter more than one: a single executed transaction proves KeeperHub
can broadcast. Two agents driving opposite actions through one shared execution
layer is what makes this a layer rather than a one-off script.

All transactions are sponsored (gas paid by the KeeperHub relay; the wallet held no
ETH) and fully verifiable on Etherscan.

WHY THIS IS A CUSTOMER-ACQUISITION STORY, NOT JUST A DEMO:
• The monitored wallet needs no ETH — a retail user can point a guardian agent at
  their existing Aave position and walk away. No funding step, no gas onboarding.
  This is what makes autonomous position management viable for many individuals and
  small businesses, not only crypto-native power users.
• Zero custody — the agent holds only an API key; signing happens inside KeeperHub's
  Turnkey wallet. This code cannot exfiltrate a key it never has. This is the first
  objection any enterprise risk team raises, and KeeperHub already answers it.
• Every action is auditable — logs/audit.jsonl records each decision, each skip, and
  the reason, so the agent's full decision history can be replayed.

THE FLEET (5 agents, one execution layer, one audit pipeline):
• HealthFactorAgent — Aave V3 health factor tiering + exact repay sizing
  → aave-v3/repay, EXECUTED ON-CHAIN
• CapitalEfficiencyAgent — solves for the borrow size that keeps HF above a hard
  floor (max_debt = collateral x threshold / HF_target), applies a safety discount
  and the on-chain borrow cap → aave-v3/borrow, EXECUTED ON-CHAIN
• RebalancingAgent — PancakeSwap V3 LP out-of-range detection + new range
• YieldAgent — DefiLlama BSC APY/TVL, risk-adjusted, migrate only if uplift > 15%
• GridAgent — BSC DEX/CEX divergence + ATR, chain-anchored quoting + kill-switch

HealthFactor and CapitalEfficiency are deliberately paired: they manage the SAME
Aave position in opposite directions, with bounded responsibilities so they never
fight. CapitalEfficiency borrows only while HF stays above 1.35 and hands off
entirely below 1.30 — below that the position belongs to HealthFactor, whose job is
to repay.

The other three agents read live chain data and make real decisions, but run in
dry-run and do not broadcast. Stated plainly rather than papered over: they prove
the pattern generalizes to LP rebalancing, yield migration, and market-making.

TRUSTLESSNESS — KeeperHub's own report is not accepted as evidence:
The audit log records what KeeperHub TOLD us it did. That is self-reporting, and
self-reporting proves nothing. So every claim is re-checked against a public node
we do not control: does the transaction exist, did it actually succeed, did the
Pool emit the event the agent intended, for our wallet, for the amount the agent
computed. One command — `python scripts/run_reconcile.py` — exits non-zero on any
drift, so it drops into cron or CI. Result on the two live executions: 2/2
independently confirmed, amounts matching to zero delta.

Building it surfaced something worth knowing: KeeperHub executes through a
relayer, so on-chain `from` is the relayer EOA and `to` is KeeperHub's router —
never our wallet. Checking ownership from transaction fields fails on every
legitimate sponsored transaction; it can only be proven from the user/onBehalfOf
field inside the Pool event. The first version got this wrong and flagged both
real transactions as mismatches.

A push path is configured as well: an OpenZeppelin Monitor watches the Pool and
HMAC-signs a webhook to us. This is the open-source self-hosted Monitor —
OpenZeppelin disabled new Defender sign-ups on 2025-06-30 and retired the hosted
platform on 2026-07-01, so the OSS Monitor is the only path now. It supports any
EVM chain (Defender did not) plus Solana and Stellar. Network, monitor, trigger
and filter configs are checked in and ready to deploy; the receiver verifies
HMAC-SHA256(secret, payload+timestamp) and rejects replays outside a 5m window.

SAFETY (fail-closed by design):
• dry_run defaults on — no API key or no wallet means no broadcast, ever
• Hard kill-switch: drawdown limits, stale-data detection, consecutive-failure halts
• idempotency_key on every execution, so a retry cannot double-spend
• Full audit trail with tx hash or skip reason
```

> 📌 上面这段英文**可直接粘贴**到 DoraHacks 的 Long Description 字段。
> 若字段有长度限制，优先删 "THE FLEET" 里三个非旗舰 agent 的描述，
> 保留 use case + live proof + customer-acquisition 三段。

**Tech Stack**：
```
Python 3.13 · Aave V3 (Sepolia) · KeeperHub MCP (Streamable HTTP) ·
Turnkey Wallet · OpenZeppelin Monitor (OSS, self-hosted) ·
Etherscan · DefiLlama · DoraHacks
```

**Links**：
```
Repo      : https://github.com/jnhualu-art/keeperhub-agent-economy
Demo      : https://youtu.be/C48UpGSQJLQ
Hackathon : https://dorahacks.io/hackathon/agent-economy/detail
Wallet     : 0x1573C3d151200922375bC48012BB1f232B2cF531
Repay TX   : https://sepolia.etherscan.io/tx/0x5c32bc4c9094e96210ad2b1a4149310849c64429a1e5a003fd6192c7a8d759e9
Borrow TX  : https://sepolia.etherscan.io/tx/0x0a565f54e189515e2fcab74afa28f70b19824ddfdc4ce685d1a974cf137b8897
Seed TX    : https://sepolia.etherscan.io/tx/0x3985c67d4068e3756f04378f7f72575e63d9fbbe6ea0bb82cf08e50f1ac79587
Reconcile  : docs/reconciliation-2026-09-03.txt  (2/2 independently verified)
```

**Team**：Solo（陆俊华 / [@Jhhu73965779](https://x.com/Jhhu73965779)）

---

## 💎 Bounty Best Feature（$1k）— 走 PR 路线，不占本 BUIDL

> **2026-09-02 修正**：bounty 不能交自己仓库的模块。
> 官方原文：*"Ship a feature as a pull request to the KeeperHub repository…
> Judged on whether we can merge it and build on it."*
> 评审第一条标准是 **Mergeability**，独立仓库无法 merge，计 0 分。
> 因此 bounty **单独立项跟踪**，与主赛道 BUIDL 无关。

### 已知门槛（2026-09-02 核实）

| 项 | 要求 |
|---|---|
| 目标分支 | **`staging`**（不是 `main`；生产分支是 `prod`） |
| Issue 关联 | **必须先有 issue**，PR 需引用（`pr-issue-link.yml` 是硬性 CI gate） |
| CI | biome lint + typecheck 必须过 |
| 测试 | 需带 vitest 单测（参考 PR #2212 的 `tests/unit/*.test.ts`） |
| 仓库活跃度 | 极高，每天合 PR —— 选题要快，晚了会被抢 |

### 选题方向（按适配度排序）

| 方向 | 依据 |
|---|---|
| **新增 Flare / Coston2 链支持** | 已有 FlareKeeper 经验，熟 chainId 114；KeeperHub 的 chain-select 走 DB 动态读，加链有标准套路 |
| **aave-v3 动作补齐**（supply / borrow / withdraw） | 目前只有 `repay`；HealthFactorAgent 正是这些动作的消费者，说得出真实需求 |
| 条件执行 + 审计做成原生 node | 本仓库 Executor 已有完整实现，但工作量大、易被拒 |

### ⚠️ 已失效的选题

- ~~`interestRateMode` 必填参数校验 DX 修复~~ → **PR #2212**
  `fix(execute): reject missing required protocol action params` 已于 **2026-09-02**
  被外部贡献者 Assassin859 抢先 merge（修 issue #2205：缺必填参数被静默 coerce
  成空串仍广播 → 改为提前校验返回 400 + 字段名）。
  **好消息**：证明外部人提 PR 到 KeeperHub 确实能 merge，bounty 路线是通的。

### 流程

1. Discord 拿到 jacob 的「high value developments」清单后选题。
   （截至 09-02 15:45 清单尚未发布，但其 **09:34 消息已剧透两大方向**：
   ① 吸引企业客户 / 大量个人与小企业 + 展示前所未见的 use case；
   ② 提升 trustlessness，建议深集成 OpenZeppelin Defender / Monitor / Relayer）
2. 去 KeeperHub 仓库**先开 issue 探路**（不做这步，PR 会卡 CI gate）
3. issue 有回应后再写码：fork → `feat/xxx` → 实现 + vitest 单测 → PR 到 `staging`

---

## ✅ 9/6 提交 checklist

- [x] YouTube 视频已上传：`https://youtu.be/C48UpGSQJLQ`（Unlisted）
- [x] README 已加 Demo 视频链接 + 改写成 use case 叙事
- [x] 修正 bounty 赛道说明（改为 PR 路线，不再重复提交主赛道）
- [x] 修正赛事时间表述（删除过期的「等待 Pre-registration 开放」）
- [ ] **报名状态确认**：DoraHacks 登录后 → 头像 → Profile → BUIDL 栏；
      或回赛事页看 "Register as Hacker" 按钮是否已变为已注册状态
- [ ] `video/demo.html` 决定是否保留（仓库里没有 mp4，视频托管在 YouTube）
- [ ] 9/6 12:00 CEST 提交闸门开闸 → Submit BUIDL（主赛道 Best Integration）
- [ ] 9/18 12:00 CEST 前可继续改 BUIDL

### 已完成加分项

- [x] **第二条真上链路径**（2026-09-03 完成）：新增 CapitalEfficiencyAgent，
      经同一 Executor 走 `aave-v3/borrow` 真借出，tx
      `0x0a565f54…8897`（HF 1.3809 → 1.3077，与预测完全吻合）。
      与 HealthFactorAgent 的 repay 形成一守一攻的对照 —— 两个 agent、
      两种相反动作、共用同一执行层与审计管道。
- [x] 单元测试扩到 **141 个全绿**（新增 64 个：对账逻辑、ABI 解码、webhook 验签）
- [x] **独立验证层**（2026-09-03 完成，对上 jacob 提的 trustlessness 方向）：
      - 拉取路径 `src/reconciler.py` + `src/evm.py`：拿**与 KeeperHub 无关**的
        公共节点核对每一条执行声明（存在性 / status / 事件类型 / 归属钱包 /
        金额）。**已真跑：2/2 全部核对通过**，金额零偏差。
        `python scripts/run_reconcile.py`，有分歧时退出码非 0，可直接挂 CI。
      - 推送路径 `oz-monitor/` + `src/alert_receiver.py`：OpenZeppelin 开源
        自托管 Monitor 配置已就绪（network / monitor / trigger / filter），
        接收端做 HMAC-SHA256 验签 + 5 分钟防重放窗口。
      - ⚠️ **Defender SaaS 已停服**（2025-06-30 停止新注册，2026-07-01 正式
        退役），必须走开源自托管版 —— README 与 `oz-monitor/README.md` 均已注明。
      - 踩到的真问题：KeeperHub 是中继执行，链上 `from` 是 relayer EOA、
        `to` 是 KeeperHub router，拿 tx 字段判归属会误判所有合法交易；
        归属只能从 Pool 事件的 `user`/`onBehalfOf` 证明。第一版就错在这里，
        把两笔真交易全判成 MISMATCH。

### 可选加分项（时间允许）

- [ ] 让 Rebalancing / Yield / Grid 中再跑一笔链上交易
      （**注意**：Sepolia Aave 各 reserve 的 supply cap 已全部打满 ——
      USDC cap 66536 vs 已供给 42.7 亿。supply 类动作在 Sepolia 上
      对任何人都已封死，只能走 borrow / repay 或换链）
- [ ] 把 `execute_check_and_execute` 用起来：把「条件 + 动作」整体交给
      KeeperHub 原子执行，进一步坐实 "conditional execution layer" 这一定位
- [ ] 真跑一个自托管的 OpenZeppelin Monitor 实例（需要 Docker 或 cargo build，
      当前未跑；配置与接收端已就绪并已测，链上推送路径待基础设施到位后验证）
