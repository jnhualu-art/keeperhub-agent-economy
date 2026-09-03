"""探查链上状态: 钱包 USDC 余额 + aave-v3 可用 action 清单。

用法:
    python scripts/probe_state.py

目的: 为「闲置 USDC 自动供给 Aave 生息」这条第二上链路径确认前置条件:
钱包里到底有没有闲置 USDC, 以及 aave-v3/supply 是否可直接执行。
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
AUSDC = config.TOKENS["USDC"]["aToken"]


def main():
    client = KeeperHubClient()
    print(f"[*] wallet = {WALLET}")
    print(f"[*] USDC   = {USDC}")
    print(f"[*] aUSDC  = {AUSDC}\n")

    print("=== 1/4 钱包 USDC 余额 (execute_contract_call balanceOf) ===")
    try:
        res = client._call_tool("execute_contract_call", {
            "contract_address": USDC,
            "chain_id": config.CHAIN_ID,
            "function_name": "balanceOf",
            "function_args": json.dumps([WALLET]),
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:600])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 2/4 钱包 aUSDC 余额 (已供给 Aave 的部分) ===")
    try:
        res = client._call_tool("execute_contract_call", {
            "contract_address": AUSDC,
            "chain_id": config.CHAIN_ID,
            "function_name": "balanceOf",
            "function_args": json.dumps([WALLET]),
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:600])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 3/4 aave-v3 全部可用 action (search_protocol_actions) ===")
    try:
        res = client._call_tool("search_protocol_actions", {"protocol": "aave-v3"})
        if isinstance(res, dict) and "actions" in res:
            for a in res["actions"]:
                at = a.get("actionType") or a.get("type") or "?"
                d = (a.get("description") or "").split("\n")[0][:80]
                print(f"   - {at}: {d}")
        else:
            print("   ", json.dumps(res, ensure_ascii=False)[:2000])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 4/4 直连执行限额 (get_spending_limits) ===")
    try:
        res = client._call_tool("get_spending_limits", {})
        print("   ", json.dumps(res, ensure_ascii=False)[:600])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n[done]")


if __name__ == "__main__":
    main()
