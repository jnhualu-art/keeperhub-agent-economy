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
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
import requests
from web3 import Web3

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
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
# mint 事件: from == 0x0, address 需左填充到 32 bytes
ZERO_TOPIC = "0x" + "0" * 64

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
    return Web3(
        Web3.HTTPProvider(rpc_url, session=session, request_kwargs={"timeout": timeout})
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
            self.identity_address = Web3.to_checksum_address(IDENTITY_REGISTRY_MAINNET)
            self.reputation_address = Web3.to_checksum_address(REPUTATION_REGISTRY_MAINNET)
            self.chain_id = 56
        else:
            self.rpc_url = rpc_url or BSC_TESTNET_RPC or pick_rpc(BSC_TESTNET_RPCS)
            self.identity_address = Web3.to_checksum_address(IDENTITY_REGISTRY_TESTNET)
            self.reputation_address = Web3.to_checksum_address(REPUTATION_REGISTRY_TESTNET)
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
    ) -> list[tuple[int, int]]:
        """
        扫描 Transfer(from == 0x0) 事件, 即新 agent 注册(mint)。

        返回 [(agent_id, block_number), ...]
        """
        # Alchemy 免费版 eth_getLogs 限 10 区块, 自动降级
        if "alchemy.com" in self.rpc_url:
            chunk_size = 10

        if to_block == "latest":
            to_block = self.w3.eth.block_number

        found: list[tuple[int, int]] = []
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
                if chunk_size > 500:
                    chunk_size //= 2
                    continue
                start = end + 1
                continue

            for log in logs:
                # topics[3] = tokenId (indexed uint256)
                if len(log["topics"]) >= 4:
                    agent_id = int(log["topics"][3].hex(), 16)
                    found.append((agent_id, log["blockNumber"]))

            logger.info("scanned %s-%s, total mints: %s", start, end, len(found))
            start = end + 1

        return found

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
                [Web3.to_checksum_address(a) for a in clients],
                "",
                "",
            ).call()
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
        """ipfs:// -> https gateway"""
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

        best = max(scores, key=scores.get)
        confidence = min(1.0, scores[best] / 2.0)
        return best, confidence

    async def fetch_registration(
        self, client: httpx.AsyncClient, uri: str
    ) -> dict:
        """抓取并解析 registration file JSON"""
        if not uri:
            return {}
        url = self.resolve_uri(uri)
        try:
            resp = await client.get(url, timeout=10.0, follow_redirects=True)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug("fetch registration failed %s: %s", url, exc)
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

        mints = self.scan_minted_agents(from_block, to_block)
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
            stats[agent.category] += 1
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

    # 落盘持久化: 供 FastAPI 后端与前端消费, 避免每次重新扫链
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents_index.json")
    with open(out_path, "w", encoding="utf-8") as f:
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
    print(f"\nsaved -> {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
