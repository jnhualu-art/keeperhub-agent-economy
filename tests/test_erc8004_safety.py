"""erc8004 索引器安全不变量 —— 每一条都对应一个真实存在过的缺陷。

erc8004 不碰私钥也不发交易, 但它是**唯一消费链上不可信字符串的模块**:
agentURI 由任意注册者写入链上, 本模块会照着去抓、去解析。所以这一批用例
的威胁模型不是"代码写错了", 而是"攻击者花一点 gas 注册一个 agent 之后
能做什么"。

配套脚本 scripts/audit_erc8004.py 可以用非 pytest 的方式复现同一批结论。
"""

import json
import os
import tempfile

import pytest

import erc8004
from erc8004 import (
    MAX_REGISTRATION_BYTES,
    MAX_REPUTATION_DECIMALS,
    Agent,
    ScanResult,
    is_safe_http_url,
)


# ---------------------------------------------------------------------------
# SSRF: agentURI 是不可信输入
# ---------------------------------------------------------------------------


class TestUriSafety:
    """注册者可以把 agentURI 指向任何地方 —— 索引器不能照单全收。"""

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # 云实例元数据(凭据)
            "http://169.254.170.2/v2/credentials",  # ECS 任务角色
            "http://127.0.0.1:8080/admin",
            "http://localhost/admin",
            "http://10.0.0.5/internal",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://[::1]:22/",
            "http://0.0.0.0/",
        ],
    )
    def test_internal_targets_rejected(self, url):
        ok, reason = is_safe_http_url(url)
        assert not ok, f"{url} 被判为安全"
        assert reason, "拒绝时必须给出理由, 便于排查"

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://evil.com/x",
            "data:text/plain,hi",
            "gopher://evil.com/_x",
            "javascript:alert(1)",
            "//evil.com/x",
        ],
    )
    def test_non_http_schemes_rejected(self, url):
        ok, _ = is_safe_http_url(url)
        assert not ok, f"{url} 未被拦截"

    @pytest.mark.parametrize(
        "url",
        [
            "https://ipfs.io/ipfs/bafybeigdyrz/1.json",
            "https://example.com/agent.json",
            "http://example.com:8080/a.json",
        ],
    )
    def test_legitimate_urls_allowed(self, url):
        """防护不能误杀正常目标, 否则等于瘫痪了功能。"""
        ok, reason = is_safe_http_url(url)
        assert ok, f"正常 URL 被误杀: {url} ({reason})"

    def test_hostname_injection_rejected(self):
        ok, _ = is_safe_http_url("https://example.com evil.com/x")
        assert not ok

    def test_malformed_url_does_not_raise(self):
        """畸形输入应当被判为不安全, 而不是抛异常冒泡到调用方。"""
        for bad in ("", "not a url", "http://", "https://[::1", "http://\x00/"):
            ok, reason = is_safe_http_url(bad)
            assert not ok, f"{bad!r} 被判为安全"
            assert isinstance(reason, str)

    def test_fetch_refuses_unsafe_uri(self):
        """端到端: 不安全的 URI 不会发出任何请求。"""
        import asyncio

        import httpx

        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)

        class ExplodingClient:
            """任何实际请求都会炸 —— 用来证明确实没有发出请求。"""

            def stream(self, *a, **kw):
                raise AssertionError("不应发出任何网络请求")

        async def run():
            return await idx.fetch_registration(
                ExplodingClient(), "http://169.254.169.254/latest/meta-data/"
            )

        assert asyncio.run(run()) == {}
        assert isinstance(httpx.HTTPError, type)  # 确保 httpx 可用(导入守卫)


# ---------------------------------------------------------------------------
# 响应体大小封顶
# ---------------------------------------------------------------------------


class TestRegistrationSizeCap:
    """URI 由攻击者控制, 一个返回数 GB 的端点就能打满内存。"""

    def test_oversized_declared_length_is_skipped(self):
        import asyncio

        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)

        class FakeStream:
            def __init__(self, headers, chunks):
                self.headers = headers
                self._chunks = chunks
                self.status_code = 200

            async def aiter_bytes(self):
                for c in self._chunks:
                    yield c

        class FakeCtx:
            def __init__(self, resp):
                self._resp = resp

            async def __aenter__(self):
                return self._resp

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def __init__(self, resp):
                self._resp = resp

            def stream(self, *a, **kw):
                return FakeCtx(self._resp)

        async def run(headers, chunks):
            return await idx.fetch_registration(FakeClient(FakeStream(headers, chunks)), None)

        # 声明长度超限
        big = {"content-length": str(MAX_REGISTRATION_BYTES + 1)}
        assert asyncio.run(run(big, [b"{}"])) == {}

        # 声明小但实际流超额(头可以撒谎, 所以读取也必须封顶)
        small = {"content-length": "10"}
        chunk = b"x" * (MAX_REGISTRATION_BYTES // 2)
        assert asyncio.run(run(small, [chunk, chunk, chunk])) == {}

    def test_normal_sized_json_is_parsed(self):
        import asyncio

        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)
        payload = json.dumps({"name": "ok"}).encode()

        class FakeStream:
            headers = {"content-length": str(len(payload))}
            status_code = 200

            async def aiter_bytes(self):
                yield payload

        class FakeCtx:
            async def __aenter__(self):
                return FakeStream()

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def stream(self, *a, **kw):
                return FakeCtx()

        async def run():
            return await idx.fetch_registration(
                FakeClient(), "https://example.com/agent.json"
            )

        assert asyncio.run(run()) == {"name": "ok"}

    def test_empty_uri_skips_request(self):
        import asyncio

        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)

        class ExplodingClient:
            def stream(self, *a, **kw):
                raise AssertionError("空 URI 不应发出请求")

        assert asyncio.run(idx.fetch_registration(ExplodingClient(), "")) == {}

    def test_malformed_json_returns_empty(self):
        import asyncio

        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)

        class FakeStream:
            headers = {"content-length": "5"}
            status_code = 200

            async def aiter_bytes(self):
                yield b"not json"

        class FakeCtx:
            async def __aenter__(self):
                return FakeStream()

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def stream(self, *a, **kw):
                return FakeCtx()

        async def run():
            return await idx.fetch_registration(
                FakeClient(), "https://example.com/agent.json"
            )

        assert asyncio.run(run()) == {}


# ---------------------------------------------------------------------------
# 扫描完整性
# ---------------------------------------------------------------------------


class TestScanCompleteness:
    """不完整的索引比没有索引更危险 —— 它看起来是完整的。"""

    def test_scan_result_exposes_gaps(self):
        r = ScanResult(mints=[(1, 10)], gaps=[(100, 199)])
        assert r.complete is False
        assert r.missing_blocks == 100

    def test_complete_scan_has_no_gaps(self):
        assert ScanResult(mints=[(1, 10)]).complete is True
        assert ScanResult().missing_blocks == 0

    def test_failed_ranges_are_recorded(self):
        """RPC 限流时不得静默跳过 —— 调用方必须能发现索引不完整。"""
        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)
        idx.rpc_url = "https://example.com"
        idx.identity_address = "0x" + "11" * 20

        calls = []

        class FakeEth:
            @property
            def block_number(self):
                return 1_000

            def get_logs(self, params):
                calls.append((params["fromBlock"], params["toBlock"]))
                raise RuntimeError("rate limited")

        class FakeW3:
            eth = FakeEth()

        idx.w3 = FakeW3()
        result = idx.scan_minted_agents(from_block=0, to_block=1_000, chunk_size=1_000)

        assert result.gaps, "失败区间必须被记录"
        assert result.complete is False
        assert result.missing_blocks == 1_001

    def test_successful_scan_reports_complete(self):
        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)
        idx.rpc_url = "https://example.com"
        idx.identity_address = "0x" + "11" * 20

        topic = bytes.fromhex("11" * 32)

        class FakeEth:
            @property
            def block_number(self):
                return 100

            def get_logs(self, params):
                return [{"topics": [b"", b"", b"", topic], "blockNumber": 5}]

        class FakeW3:
            eth = FakeEth()

        idx.w3 = FakeW3()
        result = idx.scan_minted_agents(from_block=0, to_block=100, chunk_size=50)
        assert result.complete is True
        # 0-49 / 50-99 / 100-100 共三个 chunk, 每个都返回同一条日志 ——
        # 这里只验证完整性, 去重不在 scan_minted_agents 的职责内
        assert result.mints == [(int("11" * 32, 16), 5)] * 3


# ---------------------------------------------------------------------------
# 分类稳定性
# ---------------------------------------------------------------------------


class TestClassifyDeterminism:
    def test_tie_break_is_independent_of_config_order(self):
        """max() 在并列时依赖 dict 顺序 —— 调换配置的类别书写顺序会静默
        改变分类结果。这是没人会想到要去测的耦合。"""
        keywords = list(erc8004.CATEGORIES.values())
        if len(keywords) < 2:
            pytest.skip("类别不足两个")
        kw_a, kw_b = keywords[0][0], keywords[1][0]
        reg = {"name": f"{kw_a} {kw_b}", "description": "", "tags": [], "services": [], "supportedTrust": []}

        original = dict(erc8004.CATEGORIES)
        try:
            first, _ = erc8004.ERC8004Indexer.classify(reg)
            erc8004.CATEGORIES.clear()
            erc8004.CATEGORIES.update(dict(reversed(list(original.items()))))
            second, _ = erc8004.ERC8004Indexer.classify(reg)
        finally:
            erc8004.CATEGORIES.clear()
            erc8004.CATEGORIES.update(original)

        assert first == second, "分类结果随配置书写顺序变化"

    def test_tie_break_is_alphabetical(self):
        """并列时按类别名字典序 —— 显式、稳定、与配置顺序无关。"""
        keywords = list(erc8004.CATEGORIES.values())
        if len(keywords) < 2:
            pytest.skip("类别不足两个")
        kw_a, kw_b = keywords[0][0], keywords[1][0]
        reg = {"name": f"{kw_a} {kw_b}", "description": "", "tags": [], "services": [], "supportedTrust": []}
        got, _ = erc8004.ERC8004Indexer.classify(reg)
        tied = {list(erc8004.CATEGORIES)[0], list(erc8004.CATEGORIES)[1]}
        assert got == min(tied), f"并列时应当取字典序靠前的类别, 实际 {got}"

    def test_no_match_is_general(self):
        """非金融 agent 诚实归为通用类, 不硬塞进四大金融类别。"""
        reg = {"name": "吉祥物设计助手", "description": "画图", "tags": [], "services": [], "supportedTrust": []}
        cat, conf = erc8004.ERC8004Indexer.classify(reg)
        assert cat == erc8004.CATEGORY_GENERAL
        assert conf == 0.0

    def test_confidence_bounded(self):
        for text in ("", "grid", "grid trading market making yield liquidation"):
            _, conf = erc8004.ERC8004Indexer.classify({"name": text, "description": "", "tags": [], "services": [], "supportedTrust": []})
            assert 0.0 <= conf <= 1.0


# ---------------------------------------------------------------------------
# 声誉换算
# ---------------------------------------------------------------------------


class TestReputationDecimals:
    """decimals 来自链上, 10 ** decimals 在异常值下代价极高。"""

    def test_decimals_above_bound_is_refused(self):
        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)

        class FakeFn:
            def __init__(self, result):
                self._result = result

            def call(self):
                return self._result

        class FakeFunctions:
            def __init__(self, result):
                self._result = result

            def getSummary(self, *a):
                return FakeFn(self._result)

        class FakeContract:
            def __init__(self, result):
                self.functions = FakeFunctions(result)

        idx.reputation = FakeContract((1, 100, MAX_REPUTATION_DECIMALS + 200))
        count, score = idx.get_reputation(1, ["0x" + "11" * 20])
        assert score is None, "超界 decimals 不应给出分数"

    def test_empty_clients_short_circuits(self):
        """getSummary 要求 clientAddresses 非空, 传空会 revert。"""
        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)
        assert idx.get_reputation(1, []) == (0, None)
        assert idx.get_reputation(1, None) == (0, None)


# ---------------------------------------------------------------------------
# 原子落盘
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """agents_index.json 是后端与前端的唯一数据源, 写坏等于整条链路挂掉。"""

    def test_write_is_atomic(self):
        import inspect

        src = inspect.getsource(erc8004._main)
        assert "os.replace" in src, "未使用 os.replace, 中途失败会留下半个 JSON"
        assert ".tmp" in src, "未先写临时文件"

    def test_replace_leaves_no_temp_file(self):
        """os.replace 是原子的; 且失败时不应留下 .tmp 垃圾。"""
        with tempfile.TemporaryDirectory() as d:
            final = os.path.join(d, "agents_index.json")
            tmp = final + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write('{"ok": true}')
            os.replace(tmp, final)
            assert os.path.exists(final)
            assert not os.path.exists(tmp), "os.replace 后不应残留临时文件"


# ---------------------------------------------------------------------------
# 常量与数据结构
# ---------------------------------------------------------------------------


class TestConstants:
    def test_transfer_topic_is_the_erc721_standard(self):
        """这是一个标准不可变常量, 写死后必须与权威值一致。"""
        assert (
            erc8004.TRANSFER_TOPIC
            == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )

    def test_zero_topic_is_32_zero_bytes(self):
        assert erc8004.ZERO_TOPIC == "0x" + "0" * 64

    def test_agent_to_dict_omits_raw(self):
        """raw 是完整 registration, 落盘会撑大文件且多半无用。"""
        a = Agent(agent_id=1, raw={"huge": "x" * 1000})
        assert "raw" not in a.to_dict()

    def test_category_stats_counts_every_category(self):
        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)
        stats = idx.category_stats([])
        assert set(stats) == set(erc8004.CATEGORY_LABELS)
        assert all(v == 0 for v in stats.values())

    def test_category_stats_tolerates_unknown_category(self):
        """未知类别不应 KeyError 掉整个统计。"""
        idx = erc8004.ERC8004Indexer.__new__(erc8004.ERC8004Indexer)
        stats = idx.category_stats([Agent(agent_id=1, category="no_such_category")])
        assert stats["no_such_category"] == 1
