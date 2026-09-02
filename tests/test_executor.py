"""Executor (src/executor.py) 单元测试 — 执行层的路由、风控与审计。

重点覆盖 README "Safety design" 声称的四条保证:
  1. dry_run 默认开 — 无 API key 或无 wallet 时绝不广播
  2. 未知 action 类型安全跳过
  3. 真执行路径带 tx hash, MCP 失败不抛穿
  4. 每条记录落审计 (jsonl)
"""

import json

import executor as executor_module
from executor import Executor
from keeperhub_client import MCPError


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeClient:
    """替身 KeeperHubClient: 记录调用参数, 返回预设结果。"""

    def __init__(self, result=None, error=None):
        self.result = result or {"transactionHash": "0x" + "ab" * 32}
        self.error = error
        self.calls = []

    def repay(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise MCPError(self.error)
        return self.result


def _protect_action(repay_usd=13.23, **extra):
    action = {
        "type": "PROTECT",
        "level": "WARN",
        "current_hf": 1.2471,
        "target_hf": 2.0,
        "repay_usd": repay_usd,
        "repay_asset": "USDC",
    }
    action.update(extra)
    return action


# ---------------------------------------------------------------------------
# 路由与 dry_run 风控
# ---------------------------------------------------------------------------

class TestRouting:
    def test_unknown_action_type_is_skipped_safely(self):
        ex = Executor(dry_run=True)
        rec = ex.execute_action({"type": "TELEPORT", "amount": 1})
        assert rec["executed"] is False
        assert rec["plan"] is None
        assert "no handler" in rec["note"]
        assert rec["error"] is None  # 跳过不是错误

    def test_batch_returns_one_record_per_action(self):
        ex = Executor(dry_run=True)
        records = ex.execute_batch(
            [
                _protect_action(),
                {"type": "QUOTE", "orders": [{"side": "BUY", "price": 1, "size": 1}]},
                {"type": "TELEPORT"},
            ]
        )
        assert [r["type"] for r in records] == ["PROTECT", "QUOTE", "TELEPORT"]


class TestFailClosed:
    def test_no_client_forces_dry_run_even_if_asked_live(self):
        """没有 API key 时请求 live 执行 -> 强制回退 dry_run, 绝不点火。"""
        ex = Executor(dry_run=False, client=None)  # conftest 已清掉 KEEPERHUB_API_KEY
        assert ex.dry_run is True
        assert ex.client is None

    def test_client_init_failure_falls_back_to_dry_run(self, monkeypatch):
        """client 构造失败 -> 捕获并回退 dry_run, 不抛穿。"""
        monkeypatch.setenv("KEEPERHUB_API_KEY", "test-key")
        monkeypatch.setattr(
            executor_module, "KeeperHubClient", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        ex = Executor(dry_run=False, client=None)
        assert ex.dry_run is True
        assert ex.client is None


# ---------------------------------------------------------------------------
# PROTECT: 已验证的真上链路径
# ---------------------------------------------------------------------------

class TestProtect:
    def test_dry_run_plan_units_and_params(self):
        """dry_run plan 的金额换算 (USDC 6 decimals) 与必填参数。"""
        ex = Executor(dry_run=True)
        rec = ex.execute_action(_protect_action(repay_usd=13.23))
        params = rec["plan"]["params"]
        assert rec["plan"]["actionType"] == "aave-v3/repay"
        assert params["amount"] == "13230000"  # 13.23 * 1e6
        assert params["asset"] == "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8"  # Sepolia USDC
        assert params["interestRateMode"] == "2"  # KeeperHub 必填 (variable rate)
        assert rec["executed"] is False
        assert rec["tx_hash"] is None

    def test_live_execute_returns_tx_hash(self):
        fake = FakeClient()
        ex = Executor(dry_run=False, client=fake)
        rec = ex.execute_action(_protect_action())
        assert rec["executed"] is True
        assert rec["tx_hash"] == "0x" + "ab" * 32
        # 真执行必须带 interest_rate_mode=2 与幂等键
        assert fake.calls[0]["interest_rate_mode"] == "2"
        assert fake.calls[0]["idempotency_key"].startswith("protect-")

    def test_mcp_error_is_recorded_not_raised(self):
        """链上失败 -> 记录 error, 绝不抛穿整个 batch。"""
        fake = FakeClient(error="HTTP 400: invalid params")
        ex = Executor(dry_run=False, client=fake)
        rec = ex.execute_action(_protect_action())
        assert rec["executed"] is False
        assert "HTTP 400" in rec["error"]
        assert rec["tx_hash"] is None


# ---------------------------------------------------------------------------
# 其余三类 plan 生成器
# ---------------------------------------------------------------------------

class TestPlanGenerators:
    def test_rebalance_plan_is_multistep(self):
        ex = Executor(dry_run=True)
        rec = ex.execute_action(
            {
                "type": "REBALANCE",
                "token_id": 123,
                "pair": "WBNB/USDT",
                "new_tick_lower": 100,
                "new_tick_upper": 200,
                "priority": "HIGH",
            }
        )
        steps = rec["plan"]["steps"]
        assert [s["function_name"] for s in steps] == ["decreaseLiquidity", "collect", "mint"]
        assert rec["plan"]["meta"]["token_id"] == 123
        assert rec["executed"] is False  # dry_run 不点火

    def test_migrate_plan_redeem_then_deposit(self):
        ex = Executor(dry_run=True)
        rec = ex.execute_action(
            {
                "type": "MIGRATE",
                "from_pool": "A",
                "to_symbol": "B",
                "from_apy": 5,
                "to_apy": 12,
                "uplift_pct": 20,
            }
        )
        assert [s["function_name"] for s in rec["plan"]["steps"]] == ["redeem", "deposit"]

    def test_enter_plan_approve_then_deposit(self):
        ex = Executor(dry_run=True)
        rec = ex.execute_action(
            {"type": "ENTER", "symbol": "X", "project": "Y", "expected_apy": 9}
        )
        assert [s["function_name"] for s in rec["plan"]["steps"]] == ["approve", "deposit"]

    def test_quote_plan_records_not_executes(self):
        ex = Executor(dry_run=True)
        orders = [{"side": "BUY", "price": 1, "size": 1}, {"side": "SELL", "price": 2, "size": 1}]
        rec = ex.execute_action({"type": "QUOTE", "orders": orders})
        assert rec["plan"]["order_count"] == 2
        assert len(rec["plan"]["sample"]) == 2  # 最多带 2 条样例
        assert rec["executed"] is False


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

class TestAudit:
    def test_batch_writes_jsonl_audit(self, audit_path):
        ex = Executor(dry_run=True)
        ex.execute_batch([_protect_action(), {"type": "QUOTE", "orders": []}])
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["type"] == "PROTECT"
        assert first["dry_run"] is True

    def test_audit_survives_write_failure(self, audit_path, monkeypatch):
        """审计写失败 (如磁盘只读) 不应影响执行结果本身。"""
        ex = Executor(dry_run=True)
        monkeypatch.setattr(
            executor_module, "_AUDIT_PATH", str(audit_path.parent / "no-dir" / "x" / "audit.jsonl")
        )
        rec = ex.execute_batch([_protect_action()])
        assert rec[0]["type"] == "PROTECT"  # 执行结果正常返回
