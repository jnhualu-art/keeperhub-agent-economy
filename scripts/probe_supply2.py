"""第二轮探路: 用 execute_contract_call 的 simulate 精确定位 supply 失败原因。

第一轮的失败 ('Error(51)') 有两个可能:
  A) 钱包没给 Aave Pool 授权 USDC (transferFrom 会 revert)
  B) 我把 simulate 塞进了 execute_protocol_action 的 params (该工具无此参数)

本脚本用 execute_contract_call (原生支持 simulate) 直接模拟 Pool.supply,
并先查 USDC allowance, 以区分 A/B。

用法:
    python scripts/probe_supply2.py
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

    print("=== 1/3 USDC allowance(wallet -> Aave Pool) ===")
    try:
        res = client._call_tool("execute_contract_call", {
            "contract_address": USDC,
            "chain_id": config.CHAIN_ID,
            "function_name": "allowance",
            "function_args": json.dumps([WALLET, POOL]),
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:400])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 2/3 simulate Pool.supply(1 USDC) via execute_contract_call ===")
    try:
        res = client._call_tool("execute_contract_call", {
            "contract_address": POOL,
            "chain_id": config.CHAIN_ID,
            "function_name": "supply",
            "function_args": json.dumps([USDC, "1000000", WALLET, 0]),
            "simulate": True,
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:1500])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 3/3 对照: simulate 一个必定成功的 view (getUserAccountData) ===")
    try:
        res = client._call_tool("execute_contract_call", {
            "contract_address": POOL,
            "chain_id": config.CHAIN_ID,
            "function_name": "getReserveData",
            "function_args": json.dumps([USDC]),
        })
        s = json.dumps(res, ensure_ascii=False)
        print("   ", s[:1200])
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n[done]")


if __name__ == "__main__":
    main()
