"""
极简 EVM 客户端 —— 独立链上取证的底座。

为什么自己写而不用 web3.py:
  对账的意义在于"用一个 KeeperHub 无法影响的数据源去核对 KeeperHub 的说法"。
  这条路径上每多一个依赖, 就多一个可能出问题的环节。这里只需要三个 JSON-RPC
  调用, 用标准库 urllib 就够, 全部代码可审计。

为什么不用 KeeperHub 自己的 RPC:
  那就成了让被告自己取证。对账必须走第三方节点。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# 公共 Sepolia 节点, 按顺序逐个尝试, 全挂才算失败。
# 对账的结论可靠性取决于数据源, 依赖单一节点不合适的: 一个节点抽风就能让
# 整个对账跑不起来。可用 SEPOLIA_RPC_URL 覆盖成自己的节点。
DEFAULT_RPC_URLS = [
    "https://1rpc.io/sepolia",
    "https://ethereum-sepolia-rpc.publicnode.com",
    "https://sepolia.gateway.tenderly.co",
]

DEFAULT_RPC_URL = DEFAULT_RPC_URLS[0]

DEFAULT_TIMEOUT = 20

# 部分公共节点会拦掉 Python-urllib 的默认 UA (实测 publicnode 直接返回 403),
# 所以这里伪装成常规 UA。
USER_AGENT = "Mozilla/5.0 (compatible; keeperhub-agent-economy/reconciler)"


class RpcError(Exception):
    """RPC 调用失败（网络错误或 JSON-RPC 层报错）。"""


# ── keccak256 ─────────────────────────────────────────────────
# 事件 topic0 需要 keccak。环境里 keccak 的实现不止一种, 逐个尝试; 都没有时
# 回退到预计算常量表（下面这些值是全网固定的, 与实现无关）。
_KNOWN_TOPIC0: Dict[str, str] = {
    "Supply(address,address,address,uint256,uint16)":
        "0x2b627736bca15cd5381dcf80b0bf11fd197d01a037c52b927a881a10fb73ba61",
    "Withdraw(address,address,address,uint256)":
        "0x3115d1449a7b732c986cba18244e897a450f61e1bb8d589cd2e69e6c8924f9f7",
    "Borrow(address,address,address,uint256,uint8,uint256,uint16)":
        "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0",
    "Repay(address,address,address,uint256,bool)":
        "0xa534c8dbe71f871f9f3530e97a74601fea17b426cae02e1c5aee42c96c784051",
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)":
        "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286",
}


def _load_keccak():
    """按优先级找一个可用的 keccak256 实现, 都没有则返回 None。"""
    try:
        from Crypto.Hash import keccak as _keccak  # pycryptodome

        def _impl(data: bytes) -> str:
            hasher = _keccak.new(digest_bits=256)
            hasher.update(data)
            return "0x" + hasher.hexdigest()

        return _impl
    except Exception:  # noqa: BLE001 - 任何导入/初始化问题都当作不可用
        pass

    for module_path, attr in (
        ("eth_utils.crypto", "keccak"),
        ("eth_utils", "keccak"),
        ("eth_hash.auto", "keccak"),
    ):
        try:
            module = __import__(module_path, fromlist=[attr])
            fn = getattr(module, attr)

            def _impl(data: bytes, _fn=fn) -> str:
                return "0x" + _fn(data).hex()

            # 自检一次, 确认拿到的是 keccak 而不是别的哈希
            if _impl(b"") == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470":
                return _impl
        except Exception:  # noqa: BLE001
            continue

    return None


_KECCAK = _load_keccak()


def keccak256(data: bytes) -> str:
    """keccak256(data) -> 0x 前缀 hex。优先真算, 已知签名走常量表兜底。"""
    if _KECCAK is not None:
        return _KECCAK(data)
    try:
        return _KNOWN_TOPIC0[data.decode("utf-8")]
    except (KeyError, UnicodeDecodeError) as exc:
        raise RpcError(
            "no keccak backend available and value not in fallback table; "
            "install pycryptodome (pip install pycryptodome)"
        ) from exc


def event_topic0(signature: str) -> str:
    """事件签名 -> topic0。"""
    return keccak256(signature.encode("utf-8"))


# ── JSON-RPC ──────────────────────────────────────────────────
class EvmClient:
    """只做三件事: 拿交易、拿回执、拿区块高度。"""

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        rpc_urls: Optional[List[str]] = None,
    ):
        if rpc_urls:
            self.rpc_urls = list(rpc_urls)
        elif rpc_url:
            self.rpc_urls = [rpc_url]
        else:
            override = os.getenv("SEPOLIA_RPC_URL")
            self.rpc_urls = [override] if override else list(DEFAULT_RPC_URLS)
        self.rpc_url = self.rpc_urls[0]
        self.timeout = timeout
        self._request_id = 0

    def _call(self, method: str, params: List[Any]) -> Any:
        """依次尝试各节点, 第一个成功应答的就算数。全部失败才抛错。"""
        self._request_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        ).encode("utf-8")

        failures: List[str] = []
        for url in self.rpc_urls:
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                failures.append(f"{url}: {exc}")
                continue
            except json.JSONDecodeError as exc:
                failures.append(f"{url}: non-JSON response ({exc})")
                continue

            if "error" in body:
                failures.append(f"{url}: {body['error']}")
                continue

            # 记住这次成功的节点, 后续调用优先用它
            self.rpc_url = url
            return body.get("result")

        raise RpcError("all RPC endpoints failed — " + " | ".join(failures))

    def get_transaction(self, tx_hash: str) -> Optional[dict]:
        return self._call("eth_getTransactionByHash", [tx_hash])

    def get_receipt(self, tx_hash: str) -> Optional[dict]:
        return self._call("eth_getTransactionReceipt", [tx_hash])

    def get_block_number(self) -> int:
        result = self._call("eth_blockNumber", [])
        return int(result, 16) if isinstance(result, str) else int(result)


# ── 日志解码 ──────────────────────────────────────────────────
# Aave V3 Pool 事件的参数布局: (参数名, 类型, 是否 indexed)
# 来源: aave-v3-core/contracts/interfaces/IPool.sol
#
# 这里必须逐参数声明, 不能想当然地"金额在 data 第一个 word"。踩过的坑:
# Borrow 的 user 是非 indexed 参数且排在 amount 前面, 所以 amount 在
# data 的**第二个** word。写死偏移会解出一个天文数字。
_AAVE_EVENT_ABI: Dict[str, List[tuple]] = {
    "Supply": [
        ("reserve", "address", True),
        ("user", "address", False),
        ("onBehalfOf", "address", True),
        ("amount", "uint256", False),
        ("referralCode", "uint16", True),
    ],
    "Withdraw": [
        ("reserve", "address", True),
        ("user", "address", True),
        ("to", "address", True),
        ("amount", "uint256", False),
    ],
    "Borrow": [
        ("reserve", "address", True),
        ("user", "address", False),
        ("onBehalfOf", "address", True),
        ("amount", "uint256", False),
        ("interestRateMode", "uint8", False),
        ("borrowRate", "uint256", False),
        ("referralCode", "uint16", True),
    ],
    "Repay": [
        ("reserve", "address", True),
        ("user", "address", True),
        ("repayer", "address", True),
        ("amount", "uint256", False),
        ("useATokens", "bool", False),
    ],
    "LiquidationCall": [
        ("collateralAsset", "address", True),
        ("debtAsset", "address", True),
        ("user", "address", True),
        ("debtToCover", "uint256", False),
        ("liquidatedCollateralAmount", "uint256", False),
        ("liquidator", "address", False),
        ("receiveAToken", "bool", False),
    ],
}

_EVENT_NAME_BY_TOPIC0: Dict[str, str] = {
    topic0: signature.split("(")[0] for signature, topic0 in _KNOWN_TOPIC0.items()
}


def _int_from_hex(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _decode_value(raw_word: Optional[str], type_: str):
    """把一个 32 字节 word 按类型解成 Python 值。"""
    if raw_word is None:
        return None
    if type_ == "address":
        # address 在 word 里右对齐, 取后 40 个 hex 字符
        return "0x" + raw_word[-40:].lower() if len(raw_word) >= 40 else None
    number = _int_from_hex("0x" + raw_word)
    if number is None:
        return None
    if type_ == "bool":
        return number != 0
    return number


def decode_aave_log(log: dict) -> Optional[Dict[str, Any]]:
    """按 ABI 通用解码一条原始日志成 Aave V3 Pool 事件。

    indexed 参数走 topics (从 topics[1] 起), 非 indexed 参数按声明顺序
    依次占 data 段的一个 word。不是我们关心的事件则返回 None。
    """
    topics = log.get("topics") or []
    if not topics or not isinstance(topics[0], str):
        return None

    name = _EVENT_NAME_BY_TOPIC0.get(topics[0].lower())
    if name is None:
        return None

    data = log.get("data") or "0x"
    data_body = data[2:] if data.startswith("0x") else data
    data_words = [data_body[i : i + 64] for i in range(0, len(data_body), 64)]

    decoded: Dict[str, Any] = {
        "event": name,
        "address": (log.get("address") or "").lower(),
        "log_index": _int_from_hex(log.get("logIndex")),
    }

    topic_idx = 1
    data_idx = 0
    for field_name, type_, indexed in _AAVE_EVENT_ABI[name]:
        if indexed:
            raw = topics[topic_idx] if topic_idx < len(topics) else None
            # 节点返回的 topic 一般带 0x, 但对不带的也容错
            if isinstance(raw, str):
                raw = raw[2:] if raw.startswith("0x") else raw
            else:
                raw = None
            topic_idx += 1
        else:
            raw = data_words[data_idx] if data_idx < len(data_words) else None
            data_idx += 1
        decoded[field_name] = _decode_value(raw, type_)

    # 统一成 amount_base, 方便调用方不必区分事件类型
    # (LiquidationCall 的"金额"语义是 debtToCover)
    decoded["amount_base"] = (
        decoded.get("debtToCover") if name == "LiquidationCall" else decoded.get("amount")
    )
    return decoded


def event_wallet(decoded: Dict[str, Any]) -> Optional[str]:
    """这笔事件作用在的钱包（user 优先, 退到 onBehalfOf / repayer）。"""
    for field_name in ("user", "onBehalfOf", "repayer", "to"):
        value = decoded.get(field_name)
        if isinstance(value, str):
            return value
    return None
