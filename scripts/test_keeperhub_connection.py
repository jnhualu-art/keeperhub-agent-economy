"""零依赖测试: 验证 KeeperHub API Key 能连上 MCP, 并读取 Turnkey 钱包状态。

用法:
    cd D:\WorkBuddy\keeperhub-agent-economy
    PYTHONPATH=src python scripts/test_keeperhub_connection.py
"""
import os
import sys

# 1) 在 import config 之前把 .env 注入环境变量
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

from config import WALLET_ADDRESS, CHAIN_ID  # noqa: E402
from keeperhub_client import KeeperHubClient, MCPError  # noqa: E402


def main():
    print(f"[*] WALLET_ADDRESS = {WALLET_ADDRESS}")
    print(f"[*] CHAIN_ID      = {CHAIN_ID}")
    client = KeeperHubClient()
    print("[*] KeeperHubClient 初始化完成 (Key 已加载)\n")

    try:
        print("=== 1/2 ETH 余额 (web3/check-balance) ===")
        bal = client.check_balance(WALLET_ADDRESS)
        print("   ", bal)
    except MCPError as e:
        print("   [MCPError]", e)

    try:
        print("\n=== 2/2 Aave V3 仓位 (aave-v3/get-user-account-data) ===")
        data = client.get_user_account_data(WALLET_ADDRESS)
        print("   ", data)
    except MCPError as e:
        print("   [MCPError]", e)

    print("\n[done] 连接测试结束")


if __name__ == "__main__":
    main()
