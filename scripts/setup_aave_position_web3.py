"""
keeperhub-agent-economy — Plan D 兜底脚本：用 web3.py 直连 Aave V3 Sepolia 开仓 / 还债。

⚠️ 仅在你拿不到 KeeperHub API Key 时使用。这条路不经过 KeeperHub MCP 执行层，
   会弱化「Best Integration」赛道里「Agent → KeeperHub 真上链」的叙事。
   优先走 Plan C（rebalance-keeper/scripts/setup_test_position.py，经 KeeperHub MCP）。

前置依赖：
    pip install web3 python-dotenv
.env 需填：
    WALLET_ADDRESS = 0x你的钱包地址
    PRIVATE_KEY    = 0x钱包私钥（从 OKX / MetaMask 导出，仅在本地 .env，绝不 commit）
    RPC_URL        = https://rpc.sepolia.org   （可选，默认即用）

用法：
    python scripts/setup_aave_position_web3.py setup   # 存 0.05 ETH + 借 5 USDC
    python scripts/setup_aave_position_web3.py repay    # 还清全部 USDC 债务
"""
import os
import sys
import time

from web3 import Web3
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Aave V3 Sepolia 地址（来自 rebalance-keeper/src/config.py，已验证）────────
AAVE_POOL = Web3.to_checksum_address("0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951")
WETH_GATEWAY = Web3.to_checksum_address("0x387d311e47e80b498169e6fb51d3193167d89F7D")
WETH = Web3.to_checksum_address("0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c")
USDC = Web3.to_checksum_address("0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8")
USDC_DECIMALS = 6
INTEREST_RATE_MODE = 2  # 1=fixed, 2=variable
MAX_UINT = 2**256 - 1

POOL_ABI = [
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
                {"internalType": "address", "name": "onBehalfOf", "type": "address"},
                {"internalType": "uint16", "name": "referralCode", "type": "uint16"}],
     "name": "supply", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
                {"internalType": "uint256", "name": "interestRateMode", "type": "uint256"},
                {"internalType": "uint16", "name": "referralCode", "type": "uint16"},
                {"internalType": "address", "name": "onBehalfOf", "type": "address"}],
     "name": "borrow", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
                {"internalType": "uint256", "name": "interestRateMode", "type": "uint256"},
                {"internalType": "address", "name": "onBehalfOf", "type": "address"}],
     "name": "repay", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "bool", "name": "useAsCollateral", "type": "bool"}],
     "name": "setUserUseReserveAsCollateral", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "user", "type": "address"}],
     "name": "getUserAccountData", "outputs": [
        {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
        {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
        {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
        {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
        {"internalType": "uint256", "name": "ltv", "type": "uint256"},
        {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

GATEWAY_ABI = [
    {"inputs": [{"internalType": "address", "name": "onBehalfOf", "type": "address"},
                {"internalType": "uint16", "name": "referralCode", "type": "uint16"}],
     "name": "depositETH", "outputs": [], "stateMutability": "payable", "type": "function"},
]

ERC20_ABI = [
    {"inputs": [{"internalType": "address", "name": "spender", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def connect():
    rpc = os.getenv("RPC_URL", "https://rpc.sepolia.org")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        raise SystemExit(f"[x] 连不上 RPC: {rpc}")
    pk = os.getenv("PRIVATE_KEY")
    if not pk:
        raise SystemExit("[x] .env 缺少 PRIVATE_KEY")
    acct = w3.eth.account.from_key(pk)
    print(f"[*] RPC   : {rpc}")
    print(f"[*] 钱包 : {acct.address}")
    print(f"[*] 余额 : {w3.eth.get_balance(acct.address)/1e18:.4f} SepoliaETH")
    return w3, acct


def send(w3, acct, fn, value=0):
    tx = fn.build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": int(w3.eth.estimate_gas(fn(fn)) * 1.25) if False else 400000,
        "gasPrice": w3.eth.gas_price,
        "value": value,
        "chainId": 11155111,
    })
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"    -> tx {h.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    print(f"    -> status={'Success' if rcpt.status==1 else 'FAIL'}")
    return rcpt


def show_hf(w3, user):
    pool = w3.eth.contract(AAVE_POOL, abi=POOL_ABI)
    d = pool.functions.getUserAccountData(user).call()
    hf = d[5]
    hf_num = float("inf") if hf >= MAX_UINT else hf / 1e18
    print(f"\n[*] Health Factor = {hf_num}")
    print(f"[*] totalCollateralBase = ${d[0]/1e8:.2f}")
    print(f"[*] totalDebtBase      = ${d[1]/1e8:.2f}")
    return hf_num


def do_setup(w3, acct):
    user = acct.address
    gateway = w3.eth.contract(WETH_GATEWAY, abi=GATEWAY_ABI)
    pool = w3.eth.contract(AAVE_POOL, abi=POOL_ABI)
    usdc = w3.eth.contract(USDC, abi=ERC20_ABI)

    print("\n[1/4] depositETH 0.05 SepoliaETH -> WETH collateral")
    send(w3, acct, lambda: gateway.functions.depositETH(user, 0), value=w3.to_wei(0.05, "ether"))
    time.sleep(4)

    print("\n[2/4] setUserUseReserveAsCollateral(WETH, True)")
    send(w3, acct, lambda: pool.functions.setUserUseReserveAsCollateral(WETH, True))
    time.sleep(4)

    print("\n[3/4] approve USDC -> Pool (为后续 repay 准备)")
    send(w3, acct, lambda: usdc.functions.approve(AAVE_POOL, MAX_UINT))
    time.sleep(4)

    print("\n[4/4] borrow 5 USDC (variable)")
    send(w3, acct, lambda: pool.functions.borrow(USDC, 5 * 10**USDC_DECIMALS, INTEREST_RATE_MODE, 0, user))
    time.sleep(4)

    show_hf(w3, user)
    print("\n[✓] 开仓完成。Health Factor 应落在 1.2-1.5 区间（DANGER）。")


def do_repay(w3, acct):
    user = acct.address
    pool = w3.eth.contract(AAVE_POOL, abi=POOL_ABI)
    usdc = w3.eth.contract(USDC, abi=ERC20_ABI)
    bal = usdc.functions.balanceOf(user).call()
    print(f"\n[*] 钱包 USDC 余额 = {bal/10**USDC_DECIMALS:.6f}")

    print("\n[1/2] approve USDC -> Pool (max)")
    send(w3, acct, lambda: usdc.functions.approve(AAVE_POOL, MAX_UINT))
    time.sleep(4)

    print("\n[2/2] repay 全部 USDC 债务 (interestRateMode=2)")
    send(w3, acct, lambda: pool.functions.repay(USDC, MAX_UINT, INTEREST_RATE_MODE, user))
    time.sleep(4)

    show_hf(w3, user)
    print("\n[✓] 还款完成。Health Factor 应回升到 ~999（无债）。")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "setup"
    w3, acct = connect()
    if mode == "setup":
        do_setup(w3, acct)
    elif mode == "repay":
        do_repay(w3, acct)
    else:
        raise SystemExit("用法: setup_aave_position_web3.py [setup|repay]")


if __name__ == "__main__":
    main()
