"""
ERC-8004 Agent Indexer — BNB Chain (BSC)
=========================================
Build the Era Hackathon (BNB Chain) 主赛道: AI Agent Marketplace 数据层

核心职责:
1. 扫描 BSC 上 ERC-8004 Identity Registry 的 mint 事件(Transfer from 0x0), 拿到全部 agentId
2. tokenURI(agentId) 解析每个 agent 的 registration file (IPFS / HTTPS JSON)
3. 按官方四大类别自动分类:
   Rebalancing / Grid Trading / Yield Optimisation / Health Factor Monitoring
4. 读取 Reputation Registry 的链上声誉摘要 (getSummary)

官方资源:
- 合约仓库: https://github.com/bnb-chain/erc-8004-contracts
- Explorer:  https://8004scan.io
- 规范:      ERC-8004 "Trustless Agents"

作者: 陆俊华 (华Dee)
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量 — ERC-8004 官方部署地址
# ---------------------------------------------------------------------------

# BSC 公共 RPC 候选列表 — 按国内直连可用性排序(首选项已实测连通)
# 注: 官方 bsc-dataseed.bnb.org 在国内通常不可达
BSC_MAINNET_RPCS = [
    # Alchemy 节点(可选, 在 .env 设 ALCHEMY_BSC_KEY 即启用; 免费版 getLogs 限 10 区块 → scan_minted_agents 强制 chunk=10)
    *([f"https://bnb-mainnet.g.alchemy.com/v2/{os.getenv('ALCHEMY_BSC_KEY')}"] if os.getenv("ALCHEMY_BSC_KEY") else []),
    # publicnode: 直连可用, 但历史 getLogs 大概率 403
    "https://bsc.publicnode.com",
    "https://1rpc.io/bnb",            # getLogs 限 50 区块
    "https://bsc-dataseed.bnb.org/",  # 国内通常不可达
    "https://bsc-dataseed1.defibit.io/",
]
BSC_TESTNET_RPCS = [
    "https://data-seed-prebsc-1-s1.bnbchain.org:8545/",
    "https://bsc-testnet.publicnode.com",
]
BSC_MAINNET_RPC = os.getenv("BSC_RPC", "")
BSC_TESTNET_RPC = os.getenv("BSC_TESTNET_RPC", "")

# Identity Registry — 官方部署, 所有链同址
IDENTITY_REGISTRY_MAINNET = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
IDENTITY_REGISTRY_TESTNET = "0x8004A818BFB912233c491871b3d84c89A494BD9e"

# Reputation Registry
REPUTATION_REGISTRY_MAINNET = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
REPUTATION_REGISTRY_TESTNET = "0x8004B663056A597Dffe9eCcC1965A193B7388713"

# ERC-721 Transfer(address indexed from, address indexed to, uint256 indexed tokenId)
#
# 这是一个标准不可变常量(keccak256 的事件签名), 直接写死而不是 import 时现算:
# 原先 `Web3.keccak(...)` 在模块顶层执行, 使得本模块在 web3 依赖链不完整时
# 完全无法 import —— 连 resolve_uri / classify 这些纯函数都测不了。
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
# mint 事件: from == 0x0, address 需左填充到 32 bytes
ZERO_TOPIC = "0x" + "0" * 64


# registration file 的大小上限。URI 由注册者写在链上, 是不可信输入 ——
# 一个返回数 GB 的端点就能打满索引器内存。
MAX_REGISTRATION_BYTES = 2 * 1024 * 1024  # 2 MB

# 声誉值 decimals 的上界。链上返回异常大的值时, 10 ** decimals 的构造代价
# 极高, 且结果毫无意义。
MAX_REPUTATION_DECIMALS = 18


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """内网 / 回环 / 链路本地 / 保留 / 组播 / 未指定地址一律拒绝。

    169.254.0.0/16(链路本地)尤其关键: 云主机的实例元数据服务就在这个网段,
    攻击者注册一个 agent 把 URI 指向它, 就能让索引器替他访问凭据接口。
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")

# 指向本机的常见域名。字面 IP 检查拦不住它们(它们是域名形式), 而 DNS 解析
# 校验发生在 fetch_registration 里 —— 若调用方只用 is_safe_http_url 做校验,
# 这里漏了就是一个可用的绕过。
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

# 内网域名后缀。企业内网与容器网络(.local / .internal / .localdomain)常见,
# 云厂商的内部域名也在此列。
_INTERNAL_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".lan",
    ".home.arpa",
)


def is_safe_http_url(url: str) -> tuple[bool, str]:
    """判断一个 URL 是否可以安全抓取。返回 (是否安全, 拒绝理由)。

    agentURI 是链上任意注册者写入的字符串, 属于不可信输入。本函数做三层校验:
      1) scheme 白名单 —— 只放行 http/https, 挡掉 file:// / gopher:// 等
      2) 主机名字符校验 —— 挡掉注入
      3) 目标地址校验 —— 挡掉内网与云元数据网段

    局限(诚实记录): 域名形式的绕过(如 attacker.com 解析到 127.0.0.1)需要
    解析后逐个地址校验, 见 fetch_registration 里的 _resolve_and_check。
    """
    try:
        parts = urlparse(url)
    except ValueError as exc:
        return False, f"URL 无法解析: {exc}"

    if parts.scheme not in ("http", "https"):
        return False, f"仅允许 http/https, 收到 {parts.scheme or '(无 scheme)'!r}"

    host = parts.hostname
    if not host:
        return False, "URL 缺少主机名"
    if not _HOSTNAME_RE.match(host):
        return False, f"主机名含非法字符: {host!r}"

    host_lower = host.lower().rstrip(".")
    if host_lower in _LOCAL_HOSTNAMES:
        return False, f"目标主机名 {host!r} 指向本机, 拒绝抓取"
    if host_lower.endswith(_INTERNAL_HOSTNAME_SUFFIXES):
        return False, f"目标主机名 {host!r} 属于内网域名, 拒绝抓取"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""  # 普通域名, 交给解析后校验

    if _is_blocked_ip(ip):
        return False, f"目标地址 {ip} 属于内网/保留网段, 拒绝抓取"
    return True, ""


async def _resolve_and_check(host: str, port: int) -> tuple[bool, str]:
    """解析域名并逐个校验解析结果, 防止域名指向内网。

    只做字面 IP 校验是不够的: 攻击者完全可以注册一个解析到 127.0.0.1 的域名。
    DNS rebinding(校验与请求之间记录被改)无法在应用层彻底防御, 需在网络层
    收敛, 这里只覆盖"域名本就指向内网"这一更常见的情形。
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"DNS 解析失败: {exc}"

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return False, f"域名 {host} 解析到内网地址 {ip}, 拒绝抓取"
    return True, ""


def _web3():
    """惰性导入 web3 —— 理由同 grid_agent._web3()。

    本模块只有 RPC 路径需要 web3; 分类、URI 解析这些纯函数不该被一条坏掉的
    依赖链挡在可测范围之外。
    """
    try:
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError(
            "web3 不可用, 无法发起链上调用(URI 解析与分类本身不需要 web3)"
        ) from exc
    return Web3

# ---------------------------------------------------------------------------
# 最小 ABI
# ---------------------------------------------------------------------------

IDENTITY_ABI = [
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "metadataKey", "type": "string"},
        ],
        "name": "getMetadata",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "getAgentWallet",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

REPUTATION_ABI = [
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "clientAddresses", "type": "address[]"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
        ],
        "name": "getSummary",
        "outputs": [
            {"name": "count", "type": "uint64"},
            {"name": "summaryValue", "type": "int128"},
            {"name": "summaryValueDecimals", "type": "uint8"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# ---------------------------------------------------------------------------
# 官方四大类别 — 评分红线: 四类必须同等深度, 单类别严重扣分
# ---------------------------------------------------------------------------

# 金融四类关键词 — 刻意收紧, 避免 "grid"/"quote"/"spread"/"compound"
# 这类过宽的词把通用 agent 误判成金融 agent(实测会让分类完全失真)
CATEGORIES: dict[str, list[str]] = {
    "rebalancing": [
        "rebalanc", "lp range", "lp position", "liquidity position",
        "concentrated liquidity", "reset position", "tick range",
    ],
    "grid_trading": [
        "grid trading", "grid bot", "grid strategy", "market making",
        "market-mak", "market maker", "range trading",
    ],
    "yield_optimisation": [
        "yield", "apr", "apy", "farm", "vault",
        "staking", "auto-compound", "yield optimi",
    ],
    "health_factor": [
        "health factor", "liquidation", "collateral", "borrow",
        "lending position", "lending protocol", "ltv",
    ],
}

# 实测: BSC 链上 20 万+ agent 绝大多数是通用 AI agent(写代码/做设计/数据分析),
# 官方四类金融 agent 近乎空白 —— 分类必须诚实反映这一点, 不能硬凑。
CATEGORY_GENERAL = "general"

CATEGORY_LABELS = {
    "rebalancing": "Rebalancing",
    "grid_trading": "Grid Trading",
    "yield_optimisation": "Yield Optimisation",
    "health_factor": "Health Factor Monitoring",
    "general": "General AI Agent",
    "uncategorised": "Uncategorised",
}


def make_web3(rpc_url: str, timeout: int = 25) -> Web3:
    """
    创建 Web3 实例(全项目统一入口)。

    关键: 禁用 requests 的系统代理自动发现(trust_env=False)。
    国内机器常配置 HTTP_PROXY(如 127.0.0.1:7890 的 VPN), 一旦代理断开或
    限流, 所有 RPC 调用都会抛 ProxyError:
        "Unable to connect to proxy / Remote end closed connection"
    而 BSC 公共节点(publicnode / 1rpc)实测直连可用, 因此一律直连不走代理。
    """
    session = requests.Session()
    session.trust_env = False
    _W3 = _web3()
    return _W3(
        _W3.HTTPProvider(rpc_url, session=session, request_kwargs={"timeout": timeout})
    )


def pick_rpc(candidates: list[str], timeout: int = 8) -> str:
    """
    探测第一个可用的 RPC 节点。

    国内网络环境下 BSC 公共节点经常有个别不可达(官方 bsc-dataseed 基本不通),
    生产环境必须能自动降级到可用节点。
    """
    for url in candidates:
        try:
            w3 = make_web3(url, timeout=timeout)
            if w3.is_connected():
                logger.info("picked RPC: %s", url)
                return url
        except Exception as exc:
            logger.debug("RPC %s unavailable: %s", url, exc)
            continue
    raise RuntimeError(f"no available BSC RPC among {candidates}")


# get_logs 缩小区间重试时的最小粒度。缩到这个尺寸还失败就认定该区间不可用。
MIN_SCAN_CHUNK = 500


@dataclass
class ScanResult:
    """scan_minted_agents 的结果。

    gaps 非空意味着索引不完整 —— 调用方必须据此决定是重试、换 RPC, 还是
    明确标注"数据不完整", 而不能当作扫完了。
    """

    mints: list[tuple[int, int]] = field(default_factory=list)
    gaps: list[tuple[int, int]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def missing_blocks(self) -> int:
        return sum(end - start + 1 for start, end in self.gaps)


@dataclass
class Agent:
    """一个 ERC-8004 链上 agent 的完整档案"""

    agent_id: int
    category: str = CATEGORY_GENERAL
    category_label: str = "General AI Agent"
    category_confidence: float = 0.0
    name: str = ""
    description: str = ""
    image: str = ""
    services: list[dict] = field(default_factory=list)
    supported_trust: list[str] = field(default_factory=list)
    agent_uri: str = ""
    agent_wallet: str = ""
    owner: str = ""
    reputation_count: int = 0
    reputation_score: float | None = None
    registered_at_block: int = 0
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "category": self.category,
            "category_label": self.category_label,
            "category_confidence": round(self.category_confidence, 3),
            "name": self.name,
            "description": self.description,
            "image": self.image,
            "services": self.services,
            "supported_trust": self.supported_trust,
            "agent_uri": self.agent_uri,
            "agent_wallet": self.agent_wallet,
            "owner": self.owner,
            "reputation_count": self.reputation_count,
            "reputation_score": self.reputation_score,
            "registered_at_block": self.registered_at_block,
        }


class ERC8004Indexer:
    """BSC 链上 ERC-8004 agent 索引器"""

    def __init__(self, network: str = "mainnet", rpc_url: str | None = None):
        if network not in ("mainnet", "testnet"):
            raise ValueError("network must be 'mainnet' or 'testnet'")

        self.network = network
        if network == "mainnet":
            self.rpc_url = rpc_url or BSC_MAINNET_RPC or pick_rpc(BSC_MAINNET_RPCS)
            self.identity_address = _web3().to_checksum_address(IDENTITY_REGISTRY_MAINNET)
            self.reputation_address = _web3().to_checksum_address(REPUTATION_REGISTRY_MAINNET)
            self.chain_id = 56
        else:
            self.rpc_url = rpc_url or BSC_TESTNET_RPC or pick_rpc(BSC_TESTNET_RPCS)
            self.identity_address = _web3().to_checksum_address(IDENTITY_REGISTRY_TESTNET)
            self.reputation_address = _web3().to_checksum_address(REPUTATION_REGISTRY_TESTNET)
            self.chain_id = 97

        self.w3 = make_web3(self.rpc_url)
        self.identity = self.w3.eth.contract(
            address=self.identity_address, abi=IDENTITY_ABI
        )
        self.reputation = self.w3.eth.contract(
            address=self.reputation_address, abi=REPUTATION_ABI
        )

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def get_total_supply(self) -> int | None:
        """
        已注册 agent 总数。

        注意: totalSupply() 属于 ERC721Enumerable, 而 ERC-8004 的 Identity Registry
        是 ERC721URIStorage(upgradeable), 多数部署并未实现该函数 → 此时返回 None,
        真实总数一律通过扫描 mint 事件统计。
        """
        try:
            return self.identity.functions.totalSupply().call()
        except Exception as exc:
            logger.debug("totalSupply() unavailable: %s", exc)
            return None

    def scan_minted_agents(
        self,
        from_block: int = 0,
        to_block: int | str = "latest",
        chunk_size: int = 10_000,
    ) -> ScanResult:
        """
        扫描 Transfer(from == 0x0) 事件, 即新 agent 注册(mint)。

        返回 ScanResult: 命中的 mint 列表 + 未能扫描的区块区间。

        失败区间必须返回给调用方: 公共 RPC 限流很常见, 若像原先那样只
        logger.warning 然后跳过, 索引器会**静默少数据**, 而基于它统计出来的
        "四类覆盖数"看起来完全正常 —— 一个不完整的索引比没有索引更危险,
        因为它会让人以为数据是全的。
        """
        # Alchemy 免费版 eth_getLogs 限 10 区块, 自动降级
        if "alchemy.com" in self.rpc_url:
            chunk_size = 10

        if to_block == "latest":
            to_block = self.w3.eth.block_number

        found: list[tuple[int, int]] = []
        gaps: list[tuple[int, int]] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk_size - 1, to_block)
            try:
                logs = self.w3.eth.get_logs(
                    {
                        "address": self.identity_address,
                        "fromBlock": start,
                        "toBlock": end,
                        "topics": [TRANSFER_TOPIC, ZERO_TOPIC],
                    }
                )
            except Exception as exc:  # RPC 限流很常见, 缩小区间重试
                logger.warning("get_logs failed %s-%s: %s", start, end, exc)
                if chunk_size > MIN_SCAN_CHUNK:
                    chunk_size //= 2
                    continue
                # 已缩到最小仍失败 —— 记录并跳过, 但绝不能悄无声息
                logger.error("放弃扫描区间 %s-%s, 索引将不完整", start, end)
                gaps.append((start, end))
                start = end + 1
                continue

            for log in logs:
                # topics[3] = tokenId (indexed uint256)
                if len(log["topics"]) >= 4:
                    agent_id = int(log["topics"][3].hex(), 16)
                    found.append((agent_id, log["blockNumber"]))

            logger.info("scanned %s-%s, total mints: %s", start, end, len(found))
            start = end + 1

        return ScanResult(mints=found, gaps=gaps)

    def get_agent_uri(self, agent_id: int) -> str:
        """tokenURI(agentId) — 指向 registration file"""
        try:
            return self.identity.functions.tokenURI(agent_id).call()
        except Exception as exc:
            logger.debug("tokenURI(%s) failed: %s", agent_id, exc)
            return ""

    def get_agent_wallet(self, agent_id: int) -> str:
        try:
            return self.identity.functions.getAgentWallet(agent_id).call()
        except Exception:
            return ""

    def get_reputation(self, agent_id: int, client_addresses: list[str] | None = None) -> tuple[int, float | None]:
        """
        读取链上声誉摘要。

        注意: getSummary 要求 clientAddresses 非空。
        传空列表时链上会 revert, 这里直接返回 (0, None)。
        """
        clients = client_addresses or []
        if not clients:
            return 0, None
        try:
            count, value, decimals = self.reputation.functions.getSummary(
                agent_id,
                [_web3().to_checksum_address(a) for a in clients],
                "",
                "",
            ).call()
            # decimals 来自链上, 必须设上界: 10 ** decimals 在 decimals 异常大时
            # 会构造出天文数字般的整数, 白耗内存与 CPU。ERC-8004 的声誉值用
            # uint8 decimals, 18 已远超现实需求(与 wei 精度同级)。
            if decimals > MAX_REPUTATION_DECIMALS:
                logger.warning(
                    "reputation decimals=%s 超出上界 %s, 拒绝换算 agent=%s",
                    decimals,
                    MAX_REPUTATION_DECIMALS,
                    agent_id,
                )
                return count, None
            score = value / (10 ** decimals) if decimals else float(value)
            return count, score
        except Exception as exc:
            logger.debug("getSummary(%s) failed: %s", agent_id, exc)
            return 0, None

    # ------------------------------------------------------------------
    # registration file 解析 + 分类
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_uri(uri: str) -> str:
        """ipfs:// -> https gateway

        只做协议映射, 不做安全校验 —— 校验在 is_safe_http_url 里, 两者分离
        是为了让"转换"可被单独测试, 而"是否安全"是一个独立决策。
        """
        if uri.startswith("ipfs://"):
            return uri.replace("ipfs://", "https://ipfs.io/ipfs/")
        return uri

    @staticmethod
    def classify(reg: dict) -> tuple[str, float]:
        """
        agent 分类(基于真实数据分布设计, 不是拍脑袋的关键词堆砌)。

        判定顺序:
          1) 官方四大金融类别 — 强金融关键词匹配(网格/做市/收益/清算/LP 再平衡)
          2) 都命中不了 → general(通用 AI agent)

        实测依据: 抽样 BSC 最近注册的 agent, TermiX 平台的
        termix.profile.category 分布为 Code & Smart Contracts 64% /
        Data & Research / Design & Brand / Automation & Ops,
        tags 里 AI Trading 仅个位数 —— 链上四类金融 agent 近乎空白。
        这正是本项目自建四类 reference agent 的原因。

        返回 (category_key, confidence 0~1)
        """
        text = " ".join(
            [
                str(reg.get("name", "")),
                str(reg.get("description", "")),
                json.dumps(reg.get("tags", [])),
                json.dumps(reg.get("services", [])),
                json.dumps(reg.get("supportedTrust", [])),
            ]
        ).lower()

        scores: dict[str, int] = {}
        for cat, keywords in CATEGORIES.items():
            hits = sum(1 for kw in keywords if kw in text)
            if hits:
                scores[cat] = hits

        if not scores:
            # 非金融 agent —— 诚实归为通用类, 不硬塞进四大金融类别
            return CATEGORY_GENERAL, 0.0

        # 决胜规则必须显式, 不能靠 dict 顺序: max(scores, key=scores.get) 在
        # 并列时返回 CATEGORIES 里靠前的那个, 于是「调换配置里的类别书写顺序」
        # 会静默改变分类结果 —— 这是一种没人会想到要去测的耦合。
        # 规则: 命中数降序, 命中数相同则按类别名字典序(稳定且与配置顺序无关)。
        best = min(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        confidence = min(1.0, scores[best] / 2.0)
        return best, confidence

    async def fetch_registration(
        self, client: httpx.AsyncClient, uri: str
    ) -> dict:
        """抓取并解析 registration file JSON。

        URI 由注册者写在链上, 是不可信输入, 因此这里做三件事:
          - 协议白名单(只放行 http/https)
          - 目标地址校验(挡掉内网与云元数据网段, 含域名解析后的地址)
          - 响应体大小封顶(防止恶意端点打满内存)

        任何一项不通过都返回 {} —— 索引器少一条记录可以接受, 替攻击者发
        请求或者被 OOM 不可接受。
        """
        if not uri:
            return {}

        url = self.resolve_uri(uri)
        ok, reason = is_safe_http_url(url)
        if not ok:
            logger.warning("拒绝抓取 agentURI %s: %s", url[:120], reason)
            return {}

        parts = urlparse(url)
        ok, reason = await _resolve_and_check(parts.hostname, parts.port or 443)
        if not ok:
            logger.warning("拒绝抓取 agentURI %s: %s", url[:120], reason)
            return {}

        try:
            async with client.stream(
                "GET", url, timeout=10.0, follow_redirects=True
            ) as resp:
                if resp.status_code != 200:
                    return {}

                # 声明长度先挡一道, 避免明知过大还要读
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_REGISTRATION_BYTES:
                    logger.warning(
                        "registration 过大(%s bytes), 跳过 %s", declared, url[:120]
                    )
                    return {}

                # 声明长度不可信, 实际读取也必须封顶
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_REGISTRATION_BYTES:
                        logger.warning(
                            "registration 超过 %s bytes, 中止读取 %s",
                            MAX_REGISTRATION_BYTES,
                            url[:120],
                        )
                        return {}
                    chunks.append(chunk)

            return json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            logger.debug("fetch registration failed %s: %s", url[:120], exc)
            return {}

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def build_index(
        self,
        from_block: int = 0,
        to_block: int | str = "latest",
        limit: int | None = 500,
        concurrency: int = 20,
        with_reputation: bool = False,
    ) -> list[Agent]:
        """
        完整索引流程: 扫描 mint -> 取 URI -> 抓 registration -> 分类

        :param limit: 限制处理数量(MVP 阶段先跑小样本, 全量 20万+ 再放开)
        """
        logger.info("connecting to BSC %s via %s ...", self.network, self.rpc_url)
        if not self.is_connected:
            raise RuntimeError(f"cannot connect to RPC: {self.rpc_url}")

        scan = self.scan_minted_agents(from_block, to_block)
        mints = scan.mints
        if not scan.complete:
            # 不抛异常: 部分结果仍有价值。但必须让调用方看见。
            logger.error(
                "索引不完整: %s 个区块区间未能扫描(共 %s 个区块), "
                "统计结果不可当作全量",
                len(scan.gaps),
                scan.missing_blocks,
            )
        logger.info("found %s minted agents", len(mints))

        if limit:
            mints = mints[:limit]

        # 1) 取 tokenURI (RPC 串行, 天然限流)
        uri_map: dict[int, str] = {}
        for agent_id, _ in mints:
            uri = self.get_agent_uri(agent_id)
            if uri:
                uri_map[agent_id] = uri

        logger.info("resolved %s agent URIs", len(uri_map))

        # 2) 并发抓 registration file
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[int, dict] = {}

        async def _fetch(agent_id: int, uri: str) -> None:
            async with semaphore:
                async with httpx.AsyncClient() as client:
                    results[agent_id] = await self.fetch_registration(client, uri)

        await asyncio.gather(
            *(_fetch(aid, uri) for aid, uri in uri_map.items())
        )

        # 3) 组装 Agent + 分类
        block_map = dict(mints)
        agents: list[Agent] = []
        for agent_id, reg in results.items():
            if not reg:
                continue
            category, confidence = self.classify(reg)
            agent = Agent(
                agent_id=agent_id,
                category=category,
                category_label=CATEGORY_LABELS[category],
                category_confidence=confidence,
                name=str(reg.get("name", ""))[:200],
                description=str(reg.get("description", ""))[:2000],
                image=str(reg.get("image", "")),
                services=reg.get("services", []) or [],
                supported_trust=reg.get("supportedTrust", []) or [],
                agent_uri=uri_map.get(agent_id, ""),
                registered_at_block=block_map.get(agent_id, 0),
                raw=reg,
            )
            if with_reputation:
                agent.agent_wallet = self.get_agent_wallet(agent_id)
                count, score = self.get_reputation(agent_id, [agent.agent_wallet] if agent.agent_wallet else [])
                agent.reputation_count = count
                agent.reputation_score = score
            agents.append(agent)

        logger.info("indexed %s agents with registration files", len(agents))
        return agents

    def category_stats(self, agents: list[Agent]) -> dict[str, int]:
        """各类别覆盖数量 — 提交前用来验证'四类同等深度'"""
        stats = {key: 0 for key in CATEGORY_LABELS}
        for agent in agents:
            # 用 get 而不是直接索引: 链上数据可能出现配置里没有的类别,
            # 为了统计一个未知类别而让整个统计 KeyError 掉不值得 —— 统计
            # 是给人看的, 崩了就什么都看不到了。
            stats[agent.category] = stats.get(agent.category, 0) + 1
        return stats


# ---------------------------------------------------------------------------
# CLI 自测入口
# ---------------------------------------------------------------------------

async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    indexer = ERC8004Indexer(network=os.getenv("NETWORK", "mainnet"))

    if not indexer.is_connected:
        print("RPC 连接失败, 检查网络或换 RPC")
        return

    print(f"connected: {indexer.network} (chainId={indexer.chain_id})")
    supply = indexer.get_total_supply()
    print(
        f"total agent supply: {supply}"
        if supply is not None
        else "total agent supply: N/A (该实现无 totalSupply, 改用扫描 mint 事件统计)"
    )

    # 全量从 block 0 扫太慢(BSC 已 5000w+ 区块), 默认只扫最近 N 个区块
    latest = indexer.w3.eth.block_number
    scan_blocks = int(os.getenv("SCAN_BLOCKS", 500_000))
    from_block = max(0, latest - scan_blocks)
    print(f"scanning blocks {from_block} -> {latest} (span {scan_blocks})")

    agents = await indexer.build_index(
        from_block=from_block,
        limit=int(os.getenv("LIMIT", 100)),
        with_reputation=False,
    )
    print(f"indexed: {len(agents)}")
    print("category coverage:", json.dumps(indexer.category_stats(agents), indent=2))

    for agent in agents[:5]:
        print("-" * 60)
        print(json.dumps(agent.to_dict(), ensure_ascii=False, indent=2))

    # 落盘持久化: 供 FastAPI 后端与前端消费, 避免每次重新扫链。
    # 原子写入: 先写同目录临时文件再 os.replace。直接 open(path, "w") 覆盖的话,
    # 写到一半失败会留下半个 JSON —— 而这个文件是后端与前端的唯一数据源,
    # 损坏等于整条链路挂掉, 且下一次成功运行前无人能察觉。
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents_index.json")
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "network": indexer.network,
                    "chain_id": indexer.chain_id,
                    "rpc": indexer.rpc_url,
                    "scan_blocks": scan_blocks,
                    "total": len(agents),
                    "category_stats": indexer.category_stats(agents),
                    "agents": [a.to_dict() for a in agents],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp_path, out_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    print(f"\nsaved -> {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
