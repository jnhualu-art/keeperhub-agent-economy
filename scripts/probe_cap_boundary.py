"""二分定位 supply cap 的确切边界, 确认 Error(51) = SUPPLY_CAP_EXCEEDED。

背景: 从 reserve configuration bitmap 解出 supply cap = 66536, 但 Aave 的 cap
单位在不同版本/不同部署里可能是 whole tokens 也可能是 base units。钱包有
111.27 USDC (111269811 base units), 若 cap 是 66536 base units 则只剩
0.066 USDC 的可用额度。

本脚本用 simulate 从小到大试探, 找到"能成功"与"被 cap 拒绝"的分界线,
从而确定 cap 的真实单位与剩余额度。

用法:
    python scripts/probe_cap_boundary.py
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

# 试探点 (均为 base units, USDC 6 decimals)
PROBES = [
    ("1000", "0.001 USDC"),
    ("10000", "0.01 USDC"),
    ("50000", "0.05 USDC"),
    ("66000", "0.066 USDC"),
    ("67000", "0.067 USDC"),
    ("100000", "0.1 USDC"),
    ("1000000", "1 USDC"),
]


def main():
    client = KeeperHubClient()
    print(f"[*] supply cap (from bitmap) = 66536")
    print(f"[*] 钱包 USDC = 111269811 base units (111.27 USDC)\n")

    for amt, desc in PROBES:
        try:
            r = client._call_tool("execute_contract_call", {
                "contract_address": POOL,
                "chain_id": config.CHAIN_ID,
                "function_name": "supply",
                "function_args": json.dumps([USDC, amt, WALLET, 0]),
                "simulate": True,
            })
            s = json.dumps(r, ensure_ascii=False)
            if "wouldRevert" in s or "revertReason" in s:
                print(f"  [{desc:12} amount={amt:>9}] REVERT (51=cap?)")
            else:
                print(f"  [{desc:12} amount={amt:>9}] OK  -> {s[:160]}")
        except MCPError as e:
            msg = str(e)
            print(f"  [{desc:12} amount={amt:>9}] ERROR -> {msg[:160]}")

    print("\n[done]")


if __name__ == "__main__":
    main()
