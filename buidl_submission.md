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

This is not a plan-only demo. The full path executed a complete defensive cycle on
Aave V3 (Sepolia):

  • Borrow 27 USDC via KeeperHub   → HF 1.5668 → 1.2471 (WARN)
  • Agent computes PROTECT: repay 13.23 USDC
  • Executor routes aave-v3/repay  → HF 1.2471 → 1.3856 (recovered)

Both transactions are sponsored (gas paid by the KeeperHub relay; the wallet held no
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

THE FLEET (4 agents, one execution layer, one audit pipeline):
• HealthFactorAgent — Aave V3 health factor tiering + exact repay sizing
  → aave-v3/repay, EXECUTED ON-CHAIN
• RebalancingAgent — PancakeSwap V3 LP out-of-range detection + new range
• YieldAgent — DefiLlama BSC APY/TVL, risk-adjusted, migrate only if uplift > 15%
• GridAgent — BSC DEX/CEX divergence + ATR, chain-anchored quoting + kill-switch

Only HealthFactor has executed on testnet. The other three read live chain data and
make real decisions, but run in dry-run and do not broadcast. Stated plainly rather
than papered over: one verified execution path proves the integration; the other
three prove the pattern generalizes to LP rebalancing, yield migration, and
market-making.

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
Turnkey Wallet · Etherscan · DefiLlama · DoraHacks
```

**Links**：
```
Repo      : https://github.com/jnhualu-art/keeperhub-agent-economy
Demo      : https://youtu.be/C48UpGSQJLQ
Hackathon : https://dorahacks.io/hackathon/agent-economy/detail
Wallet    : 0x1573C3d151200922375bC48012BB1f232B2cF531
Borrow TX : https://sepolia.etherscan.io/tx/0x3985c67d4068e3756f04378f7f72575e63d9fbbe6ea0bb82cf08e50f1ac79587
Repay TX  : https://sepolia.etherscan.io/tx/0x5c32bc4c9094e96210ad2b1a4149310849c64429a1e5a003fd6192c7a8d759e9
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

### 可选加分项（时间允许）

- [ ] 让 Rebalancing / Yield / Grid 中**至少 1 个**也真跑一笔链上交易
      （目前只有 HealthFactor 真上链，"4 agent 舰队"卖点打折）
- [x] 补单元测试（48 个全绿：executor 风控/审计、kill-switch、HF 分级与还款额、
      interestRateMode 防回归、Sepolia 实盘场景回归；2026-09-02 完成）
- [ ] 用 OpenZeppelin Defender Monitor 给 KeeperHub 执行回执加一层监控告警
      （对上 jacob 提的 trustlessness 方向，且不需要改 KeeperHub 源码）
