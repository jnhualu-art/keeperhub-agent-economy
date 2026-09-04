"""安全审计实证脚本 — 不靠推理, 靠跑出来的结果下结论。

每个 probe 返回 (是否命中问题, 证据)。跑法:
    PYTHONPATH=src python scripts/audit_probe.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

results = []


def probe(name, fn):
    try:
        hit, evidence = fn()
    except Exception as exc:  # probe 自身炸了也算发现
        hit, evidence = True, f"probe raised: {type(exc).__name__}: {exc}"
    results.append((name, hit, evidence))


# ── 1. 熔断后 action 是否泄漏到执行层 ────────────────────────────
def p_killswitch():
    from base_agent import AgentConfig, BaseAgent

    class AlwaysTradeAgent(BaseAgent):
        CATEGORY = "TEST"

        def fetch_market_data(self):
            return {"timestamp": time.time(), "available": True, "price": 100.0}

        def run_cycle(self):
            # 每轮都产出一笔交易意图
            return {"actions": [{"type": "PROTECT", "repay_usd": 10.0}],
                    "metrics": {"drawdown_pct": 99.0},  # 远超 5% 阈值 -> 立刻熔断
                    "notes": ""}

    agent = AlwaysTradeAgent(AgentConfig(agent_name="probe"))
    history = agent.run(cycles=3)

    leaked = []
    if history:
        leaked = history[-1].get("actions", [])
    return (
        len(history) > 0 and bool(leaked),
        f"cycles=3 后 history 长度={len(history)}, "
        f"最后一条快照 actions={leaked}, "
        f"kill_switch_active={agent.state.kill_switch_active}, status={agent.state.status}",
    )


# ── 2. 执行层硬上限能否被 amount_base 绕过 ───────────────────────
def p_cap_bypass():
    import executor as ex

    captured = {}

    class SpyClient:
        def borrow(self, **kw):
            captured.update(kw)
            return {"transactionHash": "0xdeadbeef"}

        def repay(self, **kw):
            captured.update(kw)
            return {"transactionHash": "0xdeadbeef"}

    e = ex.Executor(dry_run=False, client=SpyClient())
    # 上游只给 amount_base (天文数字), 不给 amount_usd -> 硬上限校验看的是 amount_usd
    action = {
        "type": "REBALANCE",
        "venue": "aave-v3",
        "sub_action": "borrow",
        "asset": "USDC",
        "amount_base": "999999999999999",  # ~10 亿 USDC
        # 故意不带 amount_usd
    }
    rec = e.execute_action(action)
    reached = captured.get("amount") is not None
    return (
        reached,
        f"amount_base={action['amount_base']} 未带 amount_usd -> "
        f"是否发到 client={reached}, executed={rec.get('executed')}, "
        f"note={rec.get('note', '')[:60]}",
    )


# ── 2b. 借后 HF 跌破 1.0 是否被拦 ───────────────────────────────
def p_hf_floor():
    import executor as ex

    captured = {}

    class SpyClient:
        def borrow(self, **kw):
            captured.update(kw)
            return {"transactionHash": "0xdeadbeef"}

    e = ex.Executor(dry_run=False, client=SpyClient())
    action = {
        "type": "REBALANCE",
        "venue": "aave-v3",
        "sub_action": "borrow",
        "asset": "USDC",
        "amount_usd": 5.0,
        "hf_after": 0.85,  # 自称会把仓位借爆
    }
    rec = e.execute_action(action)
    return (
        captured.get("amount") is not None,
        f"declared hf_after=0.85 -> 是否发到 client={captured.get('amount') is not None}, "
        f"note={rec.get('note', '')[:70]}",
    )


# ── 2c. amount_usd 与 amount_base 矛盾是否被拦 ───────────────────
def p_amount_conflict():
    import executor as ex

    captured = {}

    class SpyClient:
        def borrow(self, **kw):
            captured.update(kw)
            return {"transactionHash": "0xdeadbeef"}

    e = ex.Executor(dry_run=False, client=SpyClient())
    action = {
        "type": "REBALANCE",
        "venue": "aave-v3",
        "sub_action": "borrow",
        "asset": "USDC",
        "amount_usd": 5.0,          # 声称 5 USDC
        "amount_base": "500000000",  # 实为 500 USDC, 差 100 倍
    }
    rec = e.execute_action(action)
    return (
        captured.get("amount") is not None,
        f"amount_usd=5.0 但 amount_base=500000000(500 USDC) -> "
        f"是否发到 client={captured.get('amount') is not None}, "
        f"note={rec.get('note', '')[:70]}",
    )


# ── 3. 幂等键是否真的幂等 ───────────────────────────────────────
def p_idempotency():
    import executor as ex

    action = {"type": "PROTECT", "repay_usd": 13.23, "repay_asset": "USDC"}
    k1 = ex.Executor._idempotency_key("protect", action, "13230000", "USDC")
    time.sleep(1.1)
    k2 = ex.Executor._idempotency_key("protect", action, "13230000", "USDC")
    # 不同金额的 action 必须得到不同的键, 否则会误吞合法的第二笔
    k3 = ex.Executor._idempotency_key(
        "protect", {"type": "PROTECT", "repay_usd": 20.0, "repay_asset": "USDC"},
        "20000000", "USDC",
    )
    same_action_same_key = k1 == k2
    different_action_differs = k1 != k3
    ok = same_action_same_key and different_action_differs
    return (
        not ok,
        f"相同 action 间隔 1.1s -> key 相同={same_action_same_key} (需 True); "
        f"不同 action -> key 不同={different_action_differs} (需 True); k1={k1[:24]}…",
    )


# ── 4. 金额换算精度 ─────────────────────────────────────────────
def p_precision():
    import executor as ex
    from decimal import Decimal

    bad = []
    for cents in range(1, 20001):
        usd = f"{cents / 100:.2f}"
        got = ex.Executor._to_base_units(usd, 6)
        want = str(int(Decimal(usd) * 10**6))
        if got != want:
            bad.append((usd, got, want))
    return (
        bool(bad),
        f"0.01~200.00 USD 共 {len(bad)} 个金额换算有误, 例如 "
        f"{bad[:3] if bad else '无 —— 全部精确'}",
    )


# ── 5. 审计不可写时是否仍在发交易 (write-ahead) ──────────────────
def p_audit_durability():
    import executor as ex

    sent = []

    class SpyClient:
        def repay(self, **kw):
            sent.append(kw)
            return {"transactionHash": "0xfeedface"}

    import tempfile

    e = ex.Executor(dry_run=False, client=SpyClient())
    original = ex._AUDIT_PATH
    # 指向一个**已存在的目录**: open(目录, "a") 必然 IsADirectoryError。
    # 不能再用"不存在的路径" —— _audit 里的 makedirs 会把它建出来,
    # 于是审计反而成功了, 测试变成假阴性。
    ex._AUDIT_PATH = tempfile.mkdtemp(prefix="audit-blocked-")
    try:
        e.execute_batch(
            [{"type": "PROTECT", "repay_usd": 5.0},
             {"type": "PROTECT", "repay_usd": 6.0}]
        )
        return (
            len(sent) > 0,
            f"审计路径不可写时, 批次内实际向链上发出了 {len(sent)} 笔交易 "
            f"(期望 0 —— write-ahead 失败应拒绝执行)",
        )
    finally:
        ex._AUDIT_PATH = original


for nm, fn in [
    ("熔断后 action 泄漏", p_killswitch),
    ("硬上限可被 amount_base 绕过", p_cap_bypass),
    ("借后 HF 跌破 1.0 未拦截", p_hf_floor),
    ("amount_usd 与 base 矛盾未拦截", p_amount_conflict),
    ("幂等键非内容派生", p_idempotency),
    ("金额换算精度丢失", p_precision),
    ("审计不可写时交易照发", p_audit_durability),
]:
    probe(nm, fn)

print("=" * 74)
print("SECURITY AUDIT PROBE")
print("=" * 74)
for name, hit, evidence in results:
    mark = "[命中]" if hit else "[通过]"
    print(f"\n{mark} {name}")
    print(f"       {evidence}")
print("\n" + "=" * 74)
n_hit = sum(1 for _, h, _ in results if h)
print(f"命中 {n_hit}/{len(results)} 项")

# 退出码非零表示有命中 —— CI 可以直接把这个脚本当门禁跑, 不必解析它的输出。
sys.exit(1 if n_hit else 0)
