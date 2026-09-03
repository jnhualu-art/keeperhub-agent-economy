"""穷举 KeeperHub 当前全部 protocol action + 支持链 + USDC 借款额度,
为「第二条真上链路径」找可行解。

背景: Sepolia Aave V3 所有 reserve 的 supply cap 均已打满, supply 路径封死。
钱包只有 Sepolia 上的 111.27 USDC, 抵押 200 USD / 负债 119.48 USD / 可借 40.52 USD。

用法:
    python scripts/probe_options.py
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
POOL = config.AAVE_POOL
USDC = config.token_addr("USDC")


def main():
    client = KeeperHubClient()

    print("=== 1/3 全部 protocol action (按协议聚合) ===")
    try:
        res = client._call_tool("search_protocol_actions", {})
        acts = res.get("actions", [])
        by_proto: dict[str, list[str]] = {}
        for a in acts:
            at = a.get("actionType", "?")
            proto = at.split("/")[0] if "/" in at else at
            by_proto.setdefault(proto, []).append(at)
        for proto, items in sorted(by_proto.items()):
            print(f"   {proto:16} ({len(items)})")
            for i in items:
                print(f"        {i}")
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 2/3 支持的链 (list_action_schemas includeChains) ===")
    try:
        res = client._call_tool("list_action_schemas", {"includeChains": True})
        chains = res.get("chains") or res.get("supportedChains") or []
        if isinstance(chains, dict):
            for k, v in list(chains.items())[:60]:
                print(f"   {k}: {v}")
        else:
            for c in chains[:60]:
                print("   ", c)
        if not chains:
            print("   (响应里没直接给 chains, keys =", list(res.keys())[:20], ")")
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 3/3 USDC 借款额度 + simulate borrow(1 USDC) ===")
    try:
        rd = client._call_tool("execute_contract_call", {
            "contract_address": POOL,
            "chain_id": config.CHAIN_ID,
            "function_name": "getReserveData",
            "function_args": json.dumps([USDC]),
        })
        print("   ", json.dumps(rd, ensure_ascii=False)[:300])
    except MCPError as e:
        print("   [MCPError]", e)

    try:
        r = client._call_tool("execute_contract_call", {
            "contract_address": POOL,
            "chain_id": config.CHAIN_ID,
            "function_name": "borrow",
            "function_args": json.dumps([USDC, "1000000", "2", "0", WALLET]),
            "simulate": True,
        })
        s = json.dumps(r, ensure_ascii=False)
        print(f"   simulate borrow 1 USDC -> {'REVERT' if 'wouldRevert' in s else 'OK'}")
        print("   ", s[:400])
    except MCPError as e:
        print("   simulate borrow MCPError:", str(e)[:300])

    print("\n[done]")


if __name__ == "__main__":
    main()
