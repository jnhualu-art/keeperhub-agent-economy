"""解码 Aave V3 reserve configuration bitmap, 诊断 supply 为何 revert Error(51)。

Aave V3 把每个 reserve 的全部配置压进一个 uint256, 位布局(官方 ReserveConfiguration.sol):
    bits   0-15   LTV
    bits  16-31   清算阈值 (liquidation threshold)
    bits  32-47   清算奖励 (liquidation bonus)
    bits  48-55   decimals
    bit   56      是否 active
    bit   57      是否 frozen
    bit   58      是否允许借款 (borrowing enabled)
    bit   59      是否允许固定利率借款
    bit   60      是否 paused
    bit   61      isolation mode
    bit   62      siloed borrowing
    bit   63      flashloan enabled
    bits 64-79    borrowable in isolation
    bits 80-115   reserve factor
    bits 116-151  borrow cap
    bits 152-187  supply cap
    bits 188-199  liquidation protocol fee
    bits 200-211  eMode category
    bits 212-251  unbacked mint cap
    bits 252-255  debt ceiling (decimals)

用法:
    python scripts/decode_reserve_config.py [bitmap十进制]
    不传参数则实时从 Aave V3 拉 USDC 的 getReserveData 再解。
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


def bits(data: int, start: int, length: int) -> int:
    """取 bitmap 中 [start, start+length) 位"""
    return (data >> start) & ((1 << length) - 1)


def decode(data: int, label: str = "") -> None:
    print(f"\n===== {label or 'reserve config'} =====")
    print(f"raw: {data}")
    print(f"  LTV                     : {bits(data, 0, 16)}")
    print(f"  liquidation threshold   : {bits(data, 16, 16)}")
    print(f"  liquidation bonus       : {bits(data, 32, 16)}")
    print(f"  decimals                : {bits(data, 48, 8)}")
    print(f"  ACTIVE                  : {bool(bits(data, 56, 1))}")
    print(f"  FROZEN                  : {bool(bits(data, 57, 1))}")
    print(f"  BORROWING_ENABLED       : {bool(bits(data, 58, 1))}")
    print(f"  STABLE_BORROW_ENABLED   : {bool(bits(data, 59, 1))}")
    print(f"  PAUSED                  : {bool(bits(data, 60, 1))}")
    print(f"  ISOLATION_MODE          : {bool(bits(data, 61, 1))}")
    print(f"  SILOED_BORROWING        : {bool(bits(data, 62, 1))}")
    print(f"  FLASHLOAN_ENABLED       : {bool(bits(data, 63, 1))}")
    print(f"  reserve factor          : {bits(data, 80, 36)}")
    print(f"  borrow cap              : {bits(data, 116, 36)}")
    print(f"  supply cap              : {bits(data, 152, 36)}")
    print(f"  liquidation protocol fee: {bits(data, 188, 12)}")
    print(f"  eMode category          : {bits(data, 200, 12)}")


def main():
    if len(sys.argv) > 1:
        decode(int(sys.argv[1]), "from argv")
        return

    from keeperhub_client import KeeperHubClient, MCPError

    client = KeeperHubClient()
    usdc = config.token_addr("USDC")

    print("[*] 从 Aave V3 (Sepolia) 实时拉 USDC reserve 数据")
    res = client._call_tool("execute_contract_call", {
        "contract_address": config.AAVE_POOL,
        "chain_id": config.CHAIN_ID,
        "function_name": "getReserveData",
        "function_args": json.dumps([usdc]),
    })
    rd = res.get("result", {})

    decode(int(rd["configuration"]["data"]), "USDC @ Sepolia Aave V3")

    liq_rate = int(rd.get("currentLiquidityRate", 0))
    var_borrow = int(rd.get("currentVariableBorrowRate", 0))
    print(f"\n  存款年化 (currentLiquidityRate / 1e27)   : {liq_rate / 1e27 * 100:.4f}%")
    print(f"  借款年化 (currentVariableBorrowRate/1e27): {var_borrow / 1e27 * 100:.4f}%")

    print("\n[诊断]")
    d = int(rd["configuration"]["data"])
    if not bits(d, 56, 1):
        print("  -> reserve 未 ACTIVE, supply 必然失败")
    if bits(d, 57, 1):
        print("  -> reserve 被 FROZEN, supply 必然失败")
    if bits(d, 60, 1):
        print("  -> reserve 被 PAUSED, supply 必然失败")
    cap = bits(d, 152, 36)
    print(f"  -> supply cap = {cap} (0 表示无上限)")


if __name__ == "__main__":
    main()
