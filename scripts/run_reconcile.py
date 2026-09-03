#!/usr/bin/env python3
"""
独立对账入口 —— 拿第三方节点核对 KeeperHub 的执行报告。

    python scripts/run_reconcile.py              # 核对审计日志里所有声称执行过的交易
    python scripts/run_reconcile.py --json       # 输出 JSON
    python scripts/run_reconcile.py --tx 0x...   # 只核对指定交易

数据源是 SEPOLIA_RPC_URL（默认公共 Sepolia 节点），与 KeeperHub 无关。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import env as env_loader  # noqa: E402

env_loader.load()

from evm import EvmClient, RpcError  # noqa: E402
from reconciler import (  # noqa: E402
    format_report,
    load_audit_claims,
    parse_claim,
    reconcile_all,
    reconcile_claim,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非报告文本")
    parser.add_argument("--tx", action="append", help="只核对指定的交易哈希（可重复）")
    parser.add_argument("--rpc", help="覆盖 RPC 端点")
    args = parser.parse_args()

    client = EvmClient(rpc_url=args.rpc)

    # 先探一下节点活不活, 免得后面每条都超时
    try:
        head = client.get_block_number()
    except RpcError as exc:
        print(f"无法连接独立节点: {exc}", file=sys.stderr)
        print("设置 SEPOLIA_RPC_URL 环境变量换一个节点试试。", file=sys.stderr)
        return 2

    if not args.json:
        print(f"独立节点: {client.rpc_url}")
        print(f"链头高度: {head}")
        print()

    if args.tx:
        results = [
            reconcile_claim(
                {"tx_hash": tx, "action": "", "amount": None, "asset": "USDC"}, client
            )
            for tx in args.tx
        ]
    else:
        claims = load_audit_claims()
        if not claims and not args.json:
            print("审计日志里没有 executed=true 的记录。")
            return 0
        results = reconcile_all(claims, client)

    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        print(format_report(results))

    # 对账有分歧时以退出码 1 结束, 方便挂到 CI / 定时任务里
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
