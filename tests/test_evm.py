"""evm.py 测试 —— 链上取证的底座。

日志样本全部取自 Sepolia 上真实的链上回执（本项目执行过的两笔交易）,
不是编造的。金额与地址都是链上原值。
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import evm
from evm import (
    EvmClient,
    RpcError,
    _KNOWN_TOPIC0,
    decode_aave_log,
    event_topic0,
    event_wallet,
    keccak256,
)

WALLET = "0x1573c3d151200922375bc48012bb1f232b2cf531"
USDC = "0x94a9d9ac8a22534e3faca9f4e7f2e2cf85d5e4c8"
POOL = "0x6ae43d3271ff6888e7fc43fd7321a503ff738951"


def _word_address(addr: str) -> str:
    """topic / data word 里的 address: 32 字节左填充, 带 0x 前缀（与节点返回一致）。"""
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _word_uint(value: int) -> str:
    """topic 形式的无符号整数: 32 字节左填充, 带 0x 前缀。"""
    return "0x" + format(value, "064x")


def _data_word_address(addr: str) -> str:
    """data 段里的 address word: 同样左填充, 但不带 0x（data 是连续 hex）。"""
    return addr.lower().removeprefix("0x").rjust(64, "0")


def _data_word_uint(value: int) -> str:
    return format(value, "064x")


# ── 真实链上日志 ──────────────────────────────────────────────
# 取自本项目在 Sepolia 上执行过的两笔交易回执, 数值都是链上原值。
#
# data 段按 word 逐个构造而不是整段照抄: 手工复制 64 个 hex 字符极易漏位
# (第一版就少抄了 4 个前导 0, 导致 interestRateMode 解成 0), 程序化拼接
# 既能自解释每个字段, 也不会出错。

# Repay 13.23 USDC  tx 0x5c32bc4c…759e9
# ABI: Repay(address indexed reserve, address indexed user, address indexed repayer,
#            uint256 amount, bool useATokens)
REAL_REPAY_LOG = {
    "address": POOL,
    "topics": [
        _KNOWN_TOPIC0["Repay(address,address,address,uint256,bool)"],
        _word_address(USDC),    # reserve
        _word_address(WALLET),  # user
        _word_address(WALLET),  # repayer
    ],
    "data": "0x" + _data_word_uint(13_230_000) + _data_word_uint(0),  # amount, useATokens
    "logIndex": "0xa7",
}

# Borrow 6.69 USDC  tx 0x0a565f54…b8897
# ABI: Borrow(address indexed reserve, address user, address indexed onBehalfOf,
#             uint256 amount, uint8 interestRateMode, uint256 borrowRate,
#             uint16 indexed referralCode)
#
# 关键: user 是非 indexed 参数且排在 amount 前面, 所以 data 的第一个 word
# 是 user, 第二个才是 amount。写死"金额在第一个 word"会解出天文数字。
REAL_BORROW_LOG = {
    "address": POOL,
    "topics": [
        _KNOWN_TOPIC0[
            "Borrow(address,address,address,uint256,uint8,uint256,uint16)"
        ],
        _word_address(USDC),    # reserve
        _word_address(WALLET),  # onBehalfOf
        _word_uint(0),          # referralCode
    ],
    "data": "0x"
    + _data_word_address(WALLET)                             # user (word 0)
    + _data_word_uint(6_690_000)                             # amount (word 1)
    + _data_word_uint(2)                                     # interestRateMode (word 2)
    + _data_word_uint(639_927_042_342_324_973_360_046_986),  # borrowRate (word 3)
    "logIndex": "0x9b",
}


# ── keccak ────────────────────────────────────────────────────
def test_topic0_matches_precomputed_table():
    """事件签名算出的 topic0 必须与预计算常量表一致。

    这条测试的价值在于交叉验证: 环境里装了 keccak 实现时, 它校验的是
    "真算的结果 == 硬编码的常量"; 没装实现时走的正是常量表本身。两种
    情况下都应当一致, 不一致说明常量表抄错了。
    """
    for signature, expected in _KNOWN_TOPIC0.items():
        assert event_topic0(signature) == expected, f"topic0 mismatch for {signature}"


def test_keccak256_known_vector():
    """keccak256("") 的公认值。没有 keccak 后端时跳过。"""
    if evm._KECCAK is None:
        pytest.skip("no keccak backend installed")
    assert (
        keccak256(b"")
        == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_keccak256_raises_for_unknown_value_without_backend():
    """既没有 keccak 后端、值又不在常量表里时, 必须显式报错而不是静默出错。"""
    if evm._KECCAK is not None:
        pytest.skip("keccak backend available, fallback path not exercised")
    with pytest.raises(RpcError, match="no keccak backend"):
        keccak256(b"UnknownEvent(uint256)")


# ── 日志解码 ──────────────────────────────────────────────────
def test_decode_real_repay_log():
    decoded = decode_aave_log(REAL_REPAY_LOG)
    assert decoded is not None
    assert decoded["event"] == "Repay"
    assert decoded["address"] == POOL
    assert decoded["reserve"] == USDC
    assert decoded["user"] == WALLET
    assert decoded["repayer"] == WALLET
    # 13.23 USDC, 6 位精度
    assert decoded["amount"] == 13_230_000
    assert decoded["amount_base"] == 13_230_000
    assert decoded["useATokens"] is False
    assert decoded["log_index"] == 0xA7


def test_decode_real_borrow_log():
    """Borrow 的 user 是非 indexed 参数且排在 amount 前面。

    写死"金额在 data 第一个 word"会解出天文数字 —— 第一版就是这么错的。
    """
    decoded = decode_aave_log(REAL_BORROW_LOG)
    assert decoded is not None
    assert decoded["event"] == "Borrow"
    assert decoded["user"] == WALLET       # data word 0
    assert decoded["amount"] == 6_690_000  # data word 1 — 6.69 USDC
    assert decoded["amount_base"] == 6_690_000
    assert decoded["interestRateMode"] == 2  # 2 = variable rate
    assert decoded["onBehalfOf"] == WALLET
    assert decoded["referralCode"] == 0


def test_decode_ignores_unrelated_event():
    log = {
        "address": POOL,
        "topics": ["0x" + "11" * 32],  # 我们不关心的事件
        "data": "0x",
    }
    assert decode_aave_log(log) is None


def test_decode_handles_malformed_log():
    assert decode_aave_log({}) is None
    assert decode_aave_log({"topics": []}) is None
    assert decode_aave_log({"topics": [None]}) is None


def test_event_wallet_prefers_user():
    assert (
        event_wallet({"user": WALLET, "onBehalfOf": "0xabc", "repayer": "0xdef"}) == WALLET
    )


def test_event_wallet_falls_back():
    assert event_wallet({"onBehalfOf": WALLET}) == WALLET
    assert event_wallet({"repayer": WALLET}) == WALLET
    assert event_wallet({}) is None


# ── RPC 多端点回退 ────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_client_falls_back_to_next_endpoint(monkeypatch):
    """第一个节点挂掉时应该自动换下一个, 而不是直接失败。"""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", {}, None
            )
        return _FakeResponse(json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x1"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = EvmClient(rpc_urls=["https://bad.example", "https://good.example"])
    assert client.get_block_number() == 1
    assert calls == ["https://bad.example", "https://good.example"]
    # 成功的节点应被记住
    assert client.rpc_url == "https://good.example"


def test_client_raises_when_all_endpoints_fail(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = EvmClient(rpc_urls=["https://a.example", "https://b.example"])
    with pytest.raises(RpcError, match="all RPC endpoints failed"):
        client.get_block_number()


def test_client_propagates_jsonrpc_error(monkeypatch):
    """节点返回 JSON-RPC 层错误时应换下一个节点, 而不是把 error 当结果返回。"""
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(
            json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = EvmClient(rpc_urls=["https://broken.example"])
    with pytest.raises(RpcError, match="all RPC endpoints failed"):
        client.get_block_number()


def test_client_sends_user_agent(monkeypatch):
    """公共节点会拦 Python-urllib 默认 UA (实测 publicnode 直接 403)。"""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["ua"] = request.get_header("User-agent")
        return _FakeResponse(json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x1"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    EvmClient(rpc_urls=["https://x.example"]).get_block_number()
    assert seen["ua"] == evm.USER_AGENT
    assert "Python-urllib" not in seen["ua"]
