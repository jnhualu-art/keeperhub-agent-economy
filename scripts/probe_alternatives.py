"""诊断: 确认 USDC reserve 的 supply cap 是否已满, 并找一条可行的替代上链动作。

检查项:
  1. aUSDC.totalSupply (已供给总量) vs supply cap 66536 —— 确认 cap 已满
  2. 钱包在 Sepolia 上各代币余额 (USDC / WETH / LINK / DAI / USDT) —— 找可供给资产
  3. 各 reserve 的 supply cap 与已用量 —— 找还剩额度的 reserve
  4. 借款能力 availableBorrowsBase —— 评估 borrow 路径

用法:
    python scripts/probe_alternatives.py
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
from decode_reserve_config import bits  # noqa: E402

WALLET = config.WALLET_ADDRESS
POOL = config.AAVE_POOL


def call(client, addr, fn, args):
    return client._call_tool("execute_contract_call", {
        "contract_address": addr,
        "chain_id": config.CHAIN_ID,
        "function_name": fn,
        "function_args": json.dumps(args),
    })


def main():
    client = KeeperHubClient()

    print("=== 1/4 各代币钱包余额 ===")
    balances = {}
    for sym, info in config.TOKENS.items():
        try:
            r = call(client, info["address"], "balanceOf", [WALLET])
            raw = int(r.get("result", "0"))
            balances[sym] = raw
            human = raw / (10 ** info["decimals"])
            flag = "  <-- 有余额" if raw > 0 else ""
            print(f"   {sym:6} {human:>18.6f}  (raw {raw}){flag}")
        except MCPError as e:
            print(f"   {sym:6} ERROR {str(e)[:100]}")

    print("\n=== 2/4 USDC reserve: cap vs 已供给 ===")
    try:
        ts = call(client, config.TOKENS["USDC"]["aToken"], "totalSupply", [])
        supplied = int(ts.get("result", "0"))
        print(f"   aUSDC.totalSupply = {supplied} ({supplied / 1e6:.6f} USDC)")
        print(f"   supply cap        = 66536")
        print(f"   -> {'cap 已满, USDC 无法再供给' if supplied >= 66536 else 'cap 未满, 剩余 ' + str(66536 - supplied)}")
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n=== 3/4 各 reserve 剩余可供给额度 ===")
    for sym, info in config.TOKENS.items():
        if "aToken" not in info:
            continue
        try:
            rd = call(client, POOL, "getReserveData", [info["address"]])
            cfg = int(rd["result"]["configuration"]["data"])
            if not bits(cfg, 56, 1):
                print(f"   {sym:6} reserve 未 ACTIVE, 跳过")
                continue
            if bits(cfg, 57, 1) or bits(cfg, 60, 1):
                print(f"   {sym:6} reserve FROZEN/PAUSED, 跳过")
                continue
            cap = bits(cfg, 152, 36)
            ts = call(client, info["aToken"], "totalSupply", [])
            used = int(ts.get("result", "0"))
            liq = int(rd["result"].get("currentLiquidityRate", 0)) / 1e27 * 100
            if cap == 0:
                status = "无上限"
                room = "∞"
            else:
                room = cap - used
                status = f"剩余 {room}" if room > 0 else "已满"
            print(f"   {sym:6} cap={cap:<12} used={used:<20} {status:<20} 存款APY={liq:.2f}%")
        except MCPError as e:
            print(f"   {sym:6} ERROR {str(e)[:90]}")

    print("\n=== 4/4 借款能力 (getUserAccountData) ===")
    try:
        d = client.get_user_account_data(WALLET)
        coll = int(d["totalCollateralBase"]) / 1e8
        debt = int(d["totalDebtBase"]) / 1e8
        avail = int(d["availableBorrowsBase"]) / 1e8
        hf = int(d["healthFactor"]) / 1e18
        print(f"   抵押 {coll:.2f} USD | 负债 {debt:.2f} USD | 可借 {avail:.2f} USD | HF {hf:.4f}")
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n[done]")


if __name__ == "__main__":
    main()
