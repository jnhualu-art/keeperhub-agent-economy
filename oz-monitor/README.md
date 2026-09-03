# OpenZeppelin Monitor — 独立链上观测层

这一层解决一个问题：**KeeperHub 说自己执行了某笔交易，那是自证。**

`logs/audit.jsonl` 记录的是 KeeperHub 返回给我们自己的执行报告。如果它出错、
漏报、或者把失败说成成功，我们的审计日志会跟着一起错。要真正 trust-minimized，
必须有一个**我们不控制、也不由 KeeperHub 控制**的第三方，独立去看链上到底发生了什么。

这里用的是 [OpenZeppelin Monitor](https://github.com/openzeppelin/openzeppelin-monitor)
的开源自托管版本（AGPL v3）。

## ⚠️ 不要用 Defender SaaS

OpenZeppelin 已经把 Defender 托管服务下线了：

| 时间 | 事件 |
|---|---|
| 2025-06-30 | 禁用新用户注册 |
| **2026-07-01** | **Defender 正式退役** |

现在唯一可行的路径是**开源自托管版**。它不是 Defender 的替代品那么简单——
开源版可配任意 EVM 链（Defender 只支持它挑过的那几条），还支持 Solana / Stellar。

## 目录结构

```
oz-monitor/
├── networks/sepolia.json                      网络定义（RPC、chainId、确认块数）
├── monitors/aave_v3_keeperhub_execution.json  监控 Aave V3 Pool 的 5 类事件
├── triggers/keeperhub_webhook.json            webhook 通知（raw payload + HMAC 签名）
├── filters/keeperhub_execution_filter.py      自定义过滤：只放行受监控钱包的事件
└── README.md                                  本文件
```

这套配置是给**自托管的 openzeppelin-monitor** 用的，把它整个目录映射到容器的
`config/` 即可。

## 部署步骤

```bash
# 1. 拿到 Monitor 本体（Rust 写的，二选一）
git clone https://github.com/openzeppelin/openzeppelin-monitor && cd openzeppelin-monitor
#   构建: cargo build --release
#   或   : docker compose up -d

# 2. 拷配置（本目录 → Monitor 的 config/）
cp -r oz-monitor/networks/* config/networks/
cp -r oz-monitor/monitors/* config/monitors/
cp -r oz-monitor/triggers/* config/triggers/
cp -r oz-monitor/filters/*  config/filters/
chmod 644 config/filters/*.py      # 权限过松会触发安全警告

# 3. 配置环境变量（.env）
#    SEPOLIA_RPC_URL           Sepolia RPC 端点
#    MONITOR_WEBHOOK_URL       https://<你的公网地址>/alerts
#    MONITOR_WEBHOOK_SECRET    HMAC 共享密钥，自己生成

# 4. 校验 + 启动
./openzeppelin-monitor --check
./openzeppelin-monitor
```

## 监控什么

`monitors/aave_v3_keeperhub_execution.json` 盯的是 Aave V3 Sepolia Pool
`0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951` 发出的 5 类事件：

| 事件 | 为什么盯 |
|---|---|
| `Supply(address,address,address,uint256,uint16)` | 供款是否真的进了池子 |
| `Withdraw(address,address,address,uint256)` | 提款 |
| `Borrow(address,address,address,uint256,uint8,uint256,uint16)` | `interestRateMode` 是否真的是变量利率 |
| `Repay(address,address,address,uint256,bool)` | 还款金额是否和 agent 算的一致 |
| `LiquidationCall(...)` | **被清算** —— 兜底防线失守，必须告警 |

事件签名必须写成精确 Solidity 类型（`uint256` 不是 `uint`）、不带参数名、不带空格，
否则 Monitor 匹配不上。

## 两条独立的验证路径

Monitor 是**推送路径**：它看到链上事件 → webhook 推给我们 → `src/alert_receiver.py`
验签后落盘。这需要跑一个自托管实例。

`src/reconciler.py` 是**拉取路径**：不依赖 Monitor，直接去链上把交易回执拉回来，
跟 `logs/audit.jsonl` 里 KeeperHub 自证的记录逐笔对账。这条**现在就能跑**，
不需要任何额外基础设施。

两条路径看的是同一批事实，只是方向相反。都跑起来才能说"验证过"而不是"报告过"。
