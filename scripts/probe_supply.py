"""零风险探路: 用 simulate=true 验证 aave-v3/supply 参数是否可被 KeeperHub 接受。

用法:
    python scripts/probe_supply.py

目的: 在真发交易前确认 (1) supply action 的参数名与格式 (2) 钱包是否已对 Aave
Pool 授权 USDC。simulate=true 只模拟不上链, 零成本零风险。

同时取 get_plugin("web3") 的 condition/action schema, 评估
execute_check_and_execute 这条「条件执行原语」路径是否可行。
"""
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = os.path.join(_BASE, ".env")
if os.path.exists(_ENV):
    with open(_ENV, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, os.path.join(_BASE, "src"))

import config  # noqa: E402
from keeperhub_client import KeeperHubClient, MCPError  # noqa: E402

WALLET = config.WALLET_ADDRESS
USDC = config.token_addr("USDC")
POOL = config.AAVE_POOL


def main():
    client = KeeperHubClient()

    print("=== 1/3 aave-v3/supply 参数 schema (search_protocol_actions) ===")
    try:
        res = client._call_tool("search_protocol_actions", {
            "protocol": "aave-v3",
            "query": "supply",
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:2500])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 2/3 simulate 一笔 supply (1 USDC) ===")
    try:
        res = client._call_tool("execute_protocol_action", {
            "actionType": "aave-v3/supply",
            "params": {
                "network": config.CHAIN_ID,
                "asset": USDC,
                "amount": "1000000",           # 1 USDC, 6 decimals
                "onBehalfOf": WALLET,
                "referralCode": "0",
                "simulate": True,
            },
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:1500])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 3/3 execute_check_and_execute 的 condition/action schema ===")
    try:
        res = client._call_tool("get_plugin", {"pluginType": "web3"})
        s = json.dumps(res, ensure_ascii=False)
        # 只打印 check_and_execute 相关片段, 避免刷屏
        idx = s.lower().find("check_and_execute")
        if idx > 0:
            print("   ...", s[max(0, idx - 200): idx + 3000])
        else:
            print("   (未找到 check_and_execute, 完整输出前 2000 字符)")
            print("   ", s[:2000])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n[done]")


if __name__ == "__main__":
    main()
