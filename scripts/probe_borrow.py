"""验证 「资本效率再平衡」场景: 在维持 HF 安全线的前提下借出 USDC。

现状 (链上实测):
    抵押 200.00 USD | 负债 119.48 USD | 清算阈值 82.5% | HF 1.3810 | 可借 40.52 USD

风控公式:
    HF = (collateral * liquidation_threshold) / debt
    令 HF_target 为借款后必须保住的最低健康因子, 则
        max_debt   = collateral * liq_threshold / HF_target
        max_borrow = max_debt - current_debt
    再乘以 safety_factor 留缓冲, 并与链上 availableBorrows 取 min。

本脚本先用 simulate 验证候选金额, 零成本零风险。

用法:
    python scripts/probe_borrow.py
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

HF_TARGET = 1.30       # 借款后必须保住的最低健康因子
SAFETY = 0.90          # 在理论上限上再打九折留缓冲


def main():
    client = KeeperHubClient()

    d = client.get_user_account_data(WALLET)
    coll = int(d["totalCollateralBase"]) / 1e8
    debt = int(d["totalDebtBase"]) / 1e8
    avail = int(d["availableBorrowsBase"]) / 1e8
    lt = int(d["currentLiquidationThreshold"]) / 1e4
    hf = int(d["healthFactor"]) / 1e18

    print(f"[链上现状] 抵押 {coll:.2f} | 负债 {debt:.2f} | 可借 {avail:.2f} | 阈值 {lt:.2%} | HF {hf:.4f}")

    max_debt = coll * lt / HF_TARGET
    max_borrow = (max_debt - debt) * SAFETY
    borrow = min(max_borrow, avail)
    print(f"[风控计算] HF_target={HF_TARGET} safety={SAFETY}")
    print(f"           max_debt   = {max_debt:.4f} USD")
    print(f"           max_borrow = {max_borrow:.4f} USD")
    print(f"           与链上可借取 min -> borrow = {borrow:.4f} USD")

    # 向下取整到 0.01, 避免精度问题
    borrow = int(borrow * 100) / 100
    amount_base = str(int(round(borrow * 1e6)))
    new_debt = debt + borrow
    new_hf = coll * lt / new_debt
    print(f"\n[最终方案] borrow {borrow:.2f} USDC ({amount_base} base units)")
    print(f"           借款后 负债 {new_debt:.2f} / HF {new_hf:.4f}  (仍 >= {HF_TARGET})")

    print("\n=== simulate 验证 ===")
    try:
        r = client._call_tool("execute_contract_call", {
            "contract_address": POOL,
            "chain_id": config.CHAIN_ID,
            "function_name": "borrow",
            "function_args": json.dumps([USDC, amount_base, "2", "0", WALLET]),
            "simulate": True,
        })
        will_revert = r.get("wouldRevert") is True
        print(f"   borrow {borrow} USDC -> {'REVERT' if will_revert else 'OK'}")
        print(f"   gasEstimate = {r.get('gasEstimate')}")
        print(f"   full = {json.dumps(r, ensure_ascii=False)[:400]}")
    except MCPError as e:
        print("   MCPError:", str(e)[:400])

    print("\n=== 顺带: aave-v3/borrow 的 protocol action 参数要求 ===")
    try:
        res = client._call_tool("search_protocol_actions", {
            "protocol": "aave-v3", "query": "borrow",
        })
        print("   ", json.dumps(res, ensure_ascii=False)[:900])
    except MCPError as e:
        print("   [MCPError]", e)


if __name__ == "__main__":
    main()
