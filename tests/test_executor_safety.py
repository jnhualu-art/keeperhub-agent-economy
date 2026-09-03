"""执行层安全不变量 —— 每一条都对应一个真实存在过的缺陷。

这些用例守护的是"执行层不盲信上游"这条原则。它们的共同点: 上游(agent)
给出畸形或恶意的 action 时, 执行层必须拒绝, 而不是照发。

配套脚本 scripts/audit_probe.py 可以用非 pytest 的方式复现同一批结论。
"""

import time

import pytest

import executor as executor_module
from executor import Executor


class SpyClient:
    """记录所有发往链上的调用, 代替真实 KeeperHubClient。"""

    def __init__(self):
        self.calls = []

    def repay(self, **kw):
        self.calls.append(("repay", kw))
        return {"transactionHash": "0x" + "ab" * 32}

    def borrow(self, **kw):
        self.calls.append(("borrow", kw))
        return {"transactionHash": "0x" + "cd" * 32}

    @property
    def sent(self):
        return self.calls


@pytest.fixture
def live_executor():
    """一个"真会点火"的 executor, 但网络调用被 SpyClient 拦下。"""
    client = SpyClient()
    return Executor(dry_run=False, client=client), client


# ── 1. 硬上限不得被 amount_base 绕过 ─────────────────────────────
def test_hard_cap_cannot_be_bypassed_by_omitting_amount_usd(live_executor):
    """只给 amount_base、不给 amount_usd, 上限校验不能因此失效。

    原实现读 action.get("amount_usd", 0.0) 判上限, 于是缺省值 0.0 永远
    小于上限 -> 天文数字的 amount_base 直接上链。
    """
    ex, client = live_executor
    rec = ex.execute_action(
        {
            "type": "REBALANCE",
            "venue": "aave-v3",
            "sub_action": "borrow",
            "asset": "USDC",
            "amount_base": "999999999999999",  # ~10 亿 USDC
        }
    )
    assert client.sent == [], "超限借款应被拦下, 不该到达 client"
    assert rec["executed"] is False
    assert "MAX_REBALANCE_USD" in rec["note"]


def test_hard_cap_still_allows_legitimate_small_borrows(live_executor):
    """修复不能矫枉过正: 限额内的正常借款必须照常放行。"""
    ex, client = live_executor
    rec = ex.execute_action(
        {
            "type": "REBALANCE",
            "venue": "aave-v3",
            "sub_action": "borrow",
            "asset": "USDC",
            "amount_usd": 6.69,
        }
    )
    assert len(client.sent) == 1
    assert rec["executed"] is True


# ── 2. 借后健康因子地板 ─────────────────────────────────────────
def test_borrow_declaring_sub_one_health_factor_is_blocked(live_executor):
    """上游自称会把仓位借到 HF < 1.0 时, 执行层必须拒绝。

    HF < 1.0 即触发清算。执行层不查链, 但绝不接受一个自称会爆仓的请求。
    """
    ex, client = live_executor
    rec = ex.execute_action(
        {
            "type": "REBALANCE",
            "venue": "aave-v3",
            "sub_action": "borrow",
            "asset": "USDC",
            "amount_usd": 5.0,
            "hf_after": 0.85,
        }
    )
    assert client.sent == []
    assert rec["executed"] is False
    assert "liquidation floor" in rec["note"]


def test_borrow_at_hf_floor_boundary_is_allowed(live_executor):
    """HF 恰好等于 1.0 是边界, 应当放行(严格小于才拦)。"""
    ex, client = live_executor
    ex.execute_action(
        {
            "type": "REBALANCE",
            "venue": "aave-v3",
            "sub_action": "borrow",
            "asset": "USDC",
            "amount_usd": 5.0,
            "hf_after": 1.0,
        }
    )
    assert len(client.sent) == 1


# ── 3. 金额自相矛盾 ─────────────────────────────────────────────
def test_conflicting_amount_usd_and_base_is_rejected(live_executor):
    """amount_usd 与 amount_base 反推值差太多时拒绝, 不猜一个数值上链。"""
    ex, client = live_executor
    rec = ex.execute_action(
        {
            "type": "REBALANCE",
            "venue": "aave-v3",
            "sub_action": "borrow",
            "asset": "USDC",
            "amount_usd": 5.0,
            "amount_base": "500000000",  # 实为 500 USDC
        }
    )
    assert client.sent == []
    assert "偏差" in rec["note"]


# ── 4. 幂等键必须内容派生 ───────────────────────────────────────
def test_idempotency_key_is_stable_across_time_within_bucket():
    """同一个 action 相隔一段时间再执行, 键必须相同(重试才会被识别)。

    原实现用 int(time.time()), 隔一秒就是另一个键 —— docstring 宣称防重复
    上链, 实际防不住, 制造了虚假的安全感。
    """
    action = {"type": "PROTECT", "repay_usd": 13.23, "repay_asset": "USDC"}
    k1 = Executor._idempotency_key("protect", action, "13230000", "USDC")
    time.sleep(1.1)
    k2 = Executor._idempotency_key("protect", action, "13230000", "USDC")
    assert k1 == k2


def test_idempotency_key_differs_for_different_actions():
    """不同内容的 action 必须得到不同的键, 否则会误吞合法的第二笔交易。"""
    a1 = {"type": "PROTECT", "repay_usd": 13.23, "repay_asset": "USDC"}
    a2 = {"type": "PROTECT", "repay_usd": 20.00, "repay_asset": "USDC"}
    k1 = Executor._idempotency_key("protect", a1, "13230000", "USDC")
    k2 = Executor._idempotency_key("protect", a2, "20000000", "USDC")
    assert k1 != k2


def test_idempotency_key_is_passed_to_client(live_executor):
    """幂等键要真的传给 KeeperHub, 光生成不用等于没有。"""
    ex, client = live_executor
    ex.execute_action({"type": "PROTECT", "repay_usd": 13.23, "repay_asset": "USDC"})
    assert len(client.sent) == 1
    sent_kwargs = client.sent[0][1]
    assert sent_kwargs.get("idempotency_key"), "idempotency_key 没有传给 client"
    assert sent_kwargs["idempotency_key"].startswith("protect-")


# ── 5. 金额换算精度 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "usd, expected_base",
    [
        ("13.23", "13230000"),
        ("2.01", "2010000"),      # 原实现在此少 1 base unit
        ("4.02", "4020000"),
        ("8.03", "8030000"),
        ("0.07", "70000"),
        ("1999.99", "1999990000"),
    ],
)
def test_usd_to_base_units_is_exact(usd, expected_base):
    """换算必须精确。float 截断会让约 1.2% 的金额少 1 base unit。"""
    assert Executor._to_base_units(usd, 6) == expected_base


def test_to_base_units_rejects_garbage():
    for bad in ("abc", "", None, -1, float("nan"), float("inf")):
        assert Executor._to_base_units(bad, 6) is None, f"{bad!r} 应被拒绝"


# ── 6. 写前日志 (write-ahead) ───────────────────────────────────
def test_no_transaction_is_sent_when_audit_is_unwritable(tmp_path, monkeypatch):
    """审计写不进去就必须拒绝执行, 而不是发完交易再补记。

    审计日志是对账器的唯一输入。写不进去 = 这笔交易从此无法被独立验证,
    而"可被独立验证"是本项目对外宣称的核心承诺, 所以宁可停摆也不盲发。
    """
    import tempfile

    client = SpyClient()
    ex = Executor(dry_run=False, client=client)
    # 指向一个已存在的目录: open(目录, 'a') 必然失败。
    # 不能用不存在的路径 —— _audit 的 makedirs 会把它建出来, 测试变假阴性。
    monkeypatch.setattr(executor_module, "_AUDIT_PATH", tempfile.mkdtemp())

    recs = ex.execute_batch(
        [{"type": "PROTECT", "repay_usd": 5.0}, {"type": "PROTECT", "repay_usd": 6.0}]
    )
    assert client.sent == [], "审计不可写时不应发出任何交易"
    # write-ahead 失败发生在 execute_action 之前, 所以连一条记录都不会产生 ——
    # 这正是目标: 中断在任何资金动作之前, 而不是事后补记。
    assert recs == [], "write-ahead 失败应直接中止批次, 不产生任何执行记录"


def test_intent_is_recorded_before_execution(tmp_path, monkeypatch):
    """真上链前必须已经有一条 intent 落在审计里。

    否则进程在"发出交易"和"写审计"之间崩溃, 就会留下一笔链上有、审计里
    无的交易 —— 对账器看不见它, 独立验证出现盲区。
    """
    client = SpyClient()
    ex = Executor(dry_run=False, client=client)
    ex.execute_batch([{"type": "PROTECT", "repay_usd": 5.0}])

    lines = [
        __import__("json").loads(line)
        for line in open(executor_module._AUDIT_PATH, encoding="utf-8")
    ]
    intents = [r for r in lines if r.get("intent")]
    assert len(intents) == 1, "执行前应先落一条 intent"
    assert intents[0]["executed"] is False
    assert intents[0]["action"]["type"] == "PROTECT"
    # intent 必须排在结果之前
    assert lines.index(intents[0]) < len(lines) - 1


def test_dry_run_does_not_write_intent_records(tmp_path, monkeypatch):
    """dry_run 不该写 intent —— 否则审计日志会被永不发生的记录塞满。"""
    ex = Executor(dry_run=True)
    ex.execute_batch([{"type": "PROTECT", "repay_usd": 5.0}])
    lines = open(executor_module._AUDIT_PATH, encoding="utf-8").read().strip().split("\n")
    assert len(lines) == 1, f"dry_run 只应写一条结果, 实际 {len(lines)} 条"


# ── 7. 畸形 action 一律拒绝 ─────────────────────────────────────
@pytest.mark.parametrize(
    "action, why",
    [
        ({"type": "PROTECT", "repay_asset": "USDC", "repay_usd": -5}, "负数金额"),
        ({"type": "PROTECT", "repay_asset": "USDC", "repay_usd": "abc"}, "非数值"),
        ({"type": "PROTECT", "repay_asset": "NOT_A_TOKEN", "repay_usd": 5}, "未知资产"),
        ({"type": "PROTECT", "repay_asset": "USDC"}, "既无 base 也无 usd"),
        ({"type": "REBALANCE", "venue": "aave-v3", "sub_action": "borrow",
          "asset": "USDC", "amount_base": "-100"}, "负数 base"),
    ],
)
def test_malformed_actions_never_reach_the_chain(action, why, live_executor):
    ex, client = live_executor
    rec = ex.execute_action(action)
    assert client.sent == [], f"{why} 不该到达 client"
    assert rec["executed"] is False


def test_unknown_asset_is_blocked_not_crashed(live_executor):
    """未知资产要被优雅拒绝, 不能因为 config.TOKENS 的 KeyError 抛出去。"""
    ex, client = live_executor
    rec = ex.execute_action({"type": "PROTECT", "repay_asset": "FAKE", "repay_usd": 5})
    assert rec["executed"] is False
    assert "unknown asset" in rec["note"]


# ── 8. tx hash 提取容错 ─────────────────────────────────────────
@pytest.mark.parametrize(
    "response, expected",
    [
        ({"transactionHash": "0xaa"}, "0xaa"),
        ({"result": {"transactionHash": "0xbb"}}, "0xbb"),
        ({"result": {"txHash": "0xcc"}}, "0xcc"),
        ({"result": None}, None),          # 原实现在此 AttributeError
        ({"result": "not-a-dict"}, None),
        ({}, None),
        (None, None),
        ("garbage", None),
    ],
)
def test_tx_hash_extraction_survives_odd_shapes(response, expected):
    """拿不到 hash 比抛异常更糟: 连对账的入口都没有。"""
    assert Executor._extract_tx_hash(response) == expected
