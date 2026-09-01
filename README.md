# KeeperHub Agent Economy — 整合提交物

> KeeperHub 第二轮「The Agent Economy Hackathon」(DoraHacks) 整合项目
> 把 D 盘两个已验证项目 **组合成一个更强的提交物**:
> - `rebalance-keeper`（第一轮 KeeperHub 代码，含**已验证真上链**的 KeeperHub MCP 执行层）
> - `bnb-build-the-era`（BNB Agent Studio 四大金融决策 agent，**真实链上读 + 真实风控**）

## 为什么这个提交物"更强"

第一轮 KeeperHub 只做了"一个 agent（RebalanceKeeper）发一个 Aave V3 交易"。
这一轮把 **4 类金融决策智能** 接到 **同一个真上链执行层** 上，形成一个完整的 Agent 经济闭环:

| Agent | 链上读 | 决策 | KeeperHub 执行 |
|---|---|---|---|
| **HealthFactor** (`hfsentinel.agent`) | Aave V3 账户数据 | 健康因子分级 (SAFE/WARN/DANGER/CRITICAL) + 计算还款额 | `aave-v3/repay` **真上链还债** ✅ 已验证 |
| **Rebalancing** (`rangeguard.agent`) | PancakeSwap V3 仓位 tick + pool slot0 | 检测脱区间 / 近边界，算新区间 | `execute_contract_call` 再平衡 plan |
| **Yield** (`yieldpilot.agent`) | DefiLlama BSC 池 APY/TVL | 风险调整评分，提升>15% 才迁移 | `execute_contract_call` 迁移 plan |
| **Grid** (`silent-martin.agent`) | BSC DEX 价格 + CEX 背离 + ATR | 链上锚定报价 + 库存偏斜 + kill-switch | DEX 网格报价 plan |

**核心卖点（评审最看重）**：`HealthFactorAgent` 的 `PROTECT` action 经 `Executor`
路由到 KeeperHub `aave-v3/repay`，在 Aave V3 (Sepolia) 上 **真的发还款交易**——
不是模拟，不是假 tx。其余三类决策同样走同一执行层，生成可一键点火的合约调用 plan。

安全设计：
- `dry_run` 默认开启，无 API Key / 无 wallet 时 **绝不** 真发交易
- 硬 kill-switch（回撤/数据陈旧/连续异常停机）
- 每次执行带 `idempotency_key` 防重复上链
- 全量审计落 `logs/audit.jsonl`（含 tx hash 或跳过原因）

## ✅ 已验证真上链（Live On-Chain Proof）

本项目不是 demo 稿——`HealthFactorAgent → Executor → KeeperHub MCP → Aave V3 Sepolia`
整条链路已在测试网 **真实执行过一次完整兜底**，全部动作可链上核验：

**场景**：监控钱包 `0x1573C3d151200922375bC48012BB1f232B2cF531` 在 Aave V3 (Sepolia) 的仓位

| 阶段 | 动作 | 健康因子 HF | 链上证明 |
|---|---|---|---|
| ① 制造危险 | 经 KeeperHub 多借 27 USDC | 1.5668 → **1.2471** (WARN) | [`0x3985c67d…79587`](https://sepolia.etherscan.io/tx/0x3985c67d4068e3756f04378f7f72575e63d9fbbe6ea0bb82cf08e50f1ac79587) |
| ② agent 决策 | `HealthFactorAgent` 读 HF=1.2471 → 判定 WARN → 产出 PROTECT（还 13.23 USDC） | — | 审计 `logs/audit.jsonl` |
| ③ 自动兜底 | `Executor` 路由 `aave-v3/repay` 真上链 | **1.2471 → 1.3856** (恢复) | [`0x5c32bc4c…759e9`](https://sepolia.etherscan.io/tx/0x5c32bc4c9094e96210ad2b1a4149310849c64429a1e5a003fd6192c7a8d759e9) |

- 两笔交易均为 `sponsored: true`（gas 由 KeeperHub relay 代付，钱包无需持有 ETH）
- 全程 **零托管私钥**：KeeperHub 用其 Turnkey 钱包代签，agent 代码只持有 API Key
- 复现命令：`DRY_RUN=false python src/main.py`（需 `.env` 填 `KEEPERHUB_API_KEY` + `MONITOR_ADDRESS`）

## 双赛道策略（建议交两个独立 BUIDL）

1. **Best Integration ($4k 主赛道)** — 本项目本体：4 agent + KeeperHub 执行层
2. **Best Feature ($1k bounty)** — 单独突出 `Executor` 的 **条件执行 + 审计风控** 模块，
   或 `HealthFactorAgent` 的 **防清算自动还款** 特性

两个 BUIDL 共用同一仓库，提交时各自指向不同 README 章节 / demo 视频即可叠加获奖。

## 目录结构

```
keeperhub-agent-economy/
├── src/
│   ├── base_agent.py          # 四大 agent 共享基类 + ERC-8004 注册
│   ├── config.py              # Aave V3 / KeeperHub / Token 常量
│   ├── keeperhub_client.py    # KeeperHub MCP HTTP 客户端 (真上链执行层)
│   ├── executor.py            # 整合层: action -> KeeperHub 路由 + 审计 (本文件核心)
│   ├── health_factor_agent.py # Aave V3 防清算 (旗舰, 真上链 repay)
│   ├── rebalancing_agent.py   # PancakeSwap V3 LP 再平衡
│   ├── yield_agent.py         # BSC 收益路由
│   ├── grid_agent.py          # BSC 网格做市 (silent-martin 移植)
│   └── main.py                # 舰队编排入口
├── .env.example
├── requirements.txt
└── README.md
```

## 本地运行

```bash
# 1) 建 venv (D 盘, 不污染 C)
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2) dry_run demo (不需要 Key, 不真发交易)
set DRY_RUN=true
.\.venv\Scripts\python src/main.py

# 3) 真上链 (需 KeeperHub API Key + 已开通的钱包)
#    复制 .env.example -> .env 并填 KEEPERHUB_API_KEY / WALLET_ADDRESS
set DRY_RUN=false
.\.venv\Scripts\python src/main.py
```

dry_run 输出示例：
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

## 注册参赛（KeeperHub 第二轮）

赛事页: https://dorahacks.io/hackathon/agent-economy/detail

1. 用 GitHub 登录 DoraHacks → 点 **Register as Hacker**
2. 等待 **Pre-registration 开放**（约 9/1 前后，6 天后）
3. **9/6 12:00 CEST** 起开放 **Submit BUIDL** → 交本项目
   - 主赛道 Best Integration ($4k)：贴仓库 + demo 视频（HealthFactor 真还债那段）
   - bounty Best Feature ($1k)：单独突出 Executor / 防清算特性
4. **Deadline: 9/18** — 在此之前可改 BUIDL

> 注意：Submission 9/6 才开，现在先把代码跑通 + 录好 demo 视频 + 准备仓库。
```
