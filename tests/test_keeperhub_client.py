"""KeeperHubClient (src/keeperhub_client.py) 单元测试 — 单位换算与错误处理。

只测纯逻辑 (单位换算、参数组装、错误归一), 不发真实 HTTP 请求:
网络层用 monkeypatch 替换 _send / _call_tool。
"""

import pytest

import config
from keeperhub_client import KeeperHubClient, MCPError


def make_client() -> KeeperHubClient:
    return KeeperHubClient(url="https://mock/mcp", api_key="test-key")


# ---------------------------------------------------------------------------
# 构造与单位换算 (纯函数)
# ---------------------------------------------------------------------------

class TestClientBasics:
    def test_missing_api_key_rejected(self):
        with pytest.raises(ValueError, match="KEEPERHUB_API_KEY"):
            KeeperHubClient(api_key="")

    def test_to_base_converts_small_amounts(self):
        client = make_client()
        # USDC (6 decimals): 13.23 -> 13230000
        assert client._to_base("13.23", 6) == "13230000"
        # WETH (18 decimals): 0.5 -> 5e17
        assert client._to_base("0.5", 18) == str(5 * 10**17)

    def test_to_base_keeps_already_base_amounts(self):
        """>= 100 的值假定已是 base 单位, 原样返回 (避免双重换算)。"""
        client = make_client()
        assert client._to_base("13230000", 6) == "13230000"

    def test_to_base_keeps_non_numeric(self):
        client = make_client()
        assert client._to_base("not-a-number", 6) == "not-a-number"

    def test_decimals_for_known_and_unknown_tokens(self):
        client = make_client()
        usdc = config.token_addr("USDC").lower()
        assert client._decimals_for(usdc) == 6
        assert client._decimals_for("0xdeadbeef") == 18  # 未知地址按标准 18 处理


# ---------------------------------------------------------------------------
# 工具调用: 错误归一为 MCPError
# ---------------------------------------------------------------------------

class TestToolCallErrors:
    def test_tool_error_raises_mcp_error(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(
            client, "_send",
            lambda *a, **k: {"error": {"code": -32602, "message": "invalid params"}},
        )
        with pytest.raises(MCPError, match="invalid params"):
            client._call_tool("execute_protocol_action", {})

    def test_empty_response_raises_mcp_error(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_send", lambda *a, **k: None)
        with pytest.raises(MCPError, match="Empty response"):
            client._call_tool("execute_protocol_action", {})

    def test_text_content_is_parsed_as_json(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(
            client, "_send",
            lambda *a, **k: {"result": {"content": [{"type": "text", "text": '{"success": true}'}]}},
        )
        assert client._call_tool("execute_protocol_action", {}) == {"success": True}


# ---------------------------------------------------------------------------
# write 动作: 参数组装 (含 interestRateMode 必填字段)
# ---------------------------------------------------------------------------

class TestWriteActions:
    def test_repay_includes_interest_rate_mode(self, monkeypatch):
        """interestRateMode 是 KeeperHub 必填字段 (踩过的坑, 必须防回归)。"""
        client = make_client()
        captured = {}

        def fake_call(name, args):
            captured["name"] = name
            captured["params"] = args["params"]
            return {"success": True, "transactionHash": "0x01"}

        monkeypatch.setattr(client, "_call_tool", fake_call)
        res = client.repay(
            asset=config.token_addr("USDC"),
            amount="13230000",
            interest_rate_mode="2",
            idempotency_key="k1",
        )
        assert res["success"] is True
        assert captured["name"] == "execute_protocol_action"
        assert captured["params"]["interestRateMode"] == "2"
        assert captured["params"]["idempotency_key"] == "k1_0"  # retry 追加 attempt 序号

    def test_retry_appends_attempt_index(self, monkeypatch):
        client = make_client()
        keys_seen = []

        def fake_call(name, args):
            keys_seen.append(args["params"]["idempotency_key"])
            return {"success": True}

        monkeypatch.setattr(client, "_call_tool", fake_call)
        client.repay(asset="0xA", amount="1", interest_rate_mode="2", idempotency_key="k1")
        assert keys_seen == ["k1_0"]

    def test_retry_exhausts_and_raises(self, monkeypatch):
        """持续失败 -> 3 次重试后抛 MCPError, 不无限循环。"""
        client = make_client()
        attempts = []

        def fake_call(name, args):
            attempts.append(1)
            raise MCPError("transient")

        monkeypatch.setattr(client, "_call_tool", fake_call)
        monkeypatch.setattr("time.sleep", lambda s: None)  # 跳过退避等待
        with pytest.raises(MCPError, match="after 3 retries"):
            client.repay(asset="0xA", amount="1", interest_rate_mode="2")
        assert len(attempts) == 3

    def test_read_action_fails_without_success_flag(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(
            client, "_call_tool", lambda *a, **k: {"success": False, "error": "nope"}
        )
        with pytest.raises(MCPError, match="get-user-account-data failed"):
            client.get_user_account_data("0xUser")
