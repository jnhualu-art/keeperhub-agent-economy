"""reconciler.py 测试 —— 独立对账逻辑。

全部用 fake 节点, 不发任何网络请求; 但日志样本取自真实链上回执。
"""

from __future__ import annotations

import json

import pytest

import config as app_config
from evm import RpcError, _KNOWN_TOPIC0
from reconciler import (
    EXPECTED_EVENT,
    format_report,
    load_audit_claims,
    parse_claim,
    reconcile_all,
    reconcile_claim,
)
from test_evm import REAL_BORROW_LOG, REAL_REPAY_LOG, USDC, WALLET, _word_address, _word_uint

# KeeperHub 在 Sepolia 上的中继执行参与方（真实地址）
RELAYER = "0x809d8252aa4f9b8f7d9be7213855b289fe7d0444"
ROUTER = "0x5af5194b4b0909eb978e3cf1e25333852277f07d"

POOL = app_config.AAVE_POOL


# ── fake 节点 ─────────────────────────────────────────────────
class FakeNode:
    """替身 EvmClient, 只实现 reconciler 用到的 get_receipt。"""

    def __init__(self, receipt=None, error=None):
        self.receipt = receipt
        self.error = error
        self.calls = []

    def get_receipt(self, tx_hash: str):
        self.calls.append(tx_hash)
        if self.error is not None:
            raise RpcError(self.error)
        return self.receipt


def _receipt(logs, status="0x1", from_addr=RELAYER, to_addr=ROUTER):
    return {"status": status, "from": from_addr, "to": to_addr, "logs": logs}


def _repay_claim(amount=13.23, asset="USDC"):
    return {
        "tx_hash": "0x5c32bc4c" + "00" * 28,
        "action": "PROTECT",
        "amount": amount,
        "asset": asset,
        "claimed_event": "Repay",
    }


def _borrow_claim(amount=6.69, asset="USDC"):
    return {
        "tx_hash": "0x0a565f54" + "00" * 28,
        "action": "REBALANCE",
        "amount": amount,
        "asset": asset,
        "claimed_event": "Borrow",
    }


def _check(result, name):
    for item in result.checks:
        if item["check"] == name:
            return item
    return None


# ── parse_claim ───────────────────────────────────────────────
def test_parse_claim_extracts_from_note():
    record = {
        "type": "REBALANCE",
        "tx_hash": "0xabc",
        "note": "borrowed 6.69 USD USDC on Aave V3, HF 1.3809 -> 1.3077, tx=0xabc",
    }
    claim = parse_claim(record)
    assert claim["action"] == "REBALANCE"
    assert claim["amount"] == 6.69
    assert claim["asset"] == "USDC"
    assert claim["claimed_event"] == "Borrow"


def test_parse_claim_handles_repay_wording():
    record = {"type": "PROTECT", "tx_hash": "0xdef", "note": "repayed 13.23 USD USDC on Aave V3"}
    claim = parse_claim(record)
    assert claim["amount"] == 13.23
    assert claim["claimed_event"] == "Repay"


def test_parse_claim_is_idempotent():
    """重复 parse 不能把 action 弄丢 —— 第一版在 scripts 里 parse 了一次、
    reconcile_all 里又 parse 一次, 导致 action 变空。"""
    record = {"type": "PROTECT", "tx_hash": "0xabc", "note": "repayed 13.23 USD USDC"}
    once = parse_claim(record)
    twice = parse_claim(once)
    assert twice["action"] == "PROTECT"
    assert twice["amount"] == 13.23


def test_parse_claim_missing_note_still_returns_action():
    claim = parse_claim({"type": "PROTECT", "tx_hash": "0xabc", "note": ""})
    assert claim["action"] == "PROTECT"
    assert claim["amount"] is None


# ── load_audit_claims ─────────────────────────────────────────
def test_load_audit_claims_filters_to_executed(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "PROTECT", "executed": True, "tx_hash": "0xaaa"}),
                json.dumps({"type": "PROTECT", "executed": False, "tx_hash": None}),
                json.dumps({"type": "REBALANCE", "executed": True, "tx_hash": "0xbbb"}),
                "这不是 JSON",
                json.dumps({"type": "PROTECT", "executed": True}),  # 缺 tx_hash
            ]
        ),
        encoding="utf-8",
    )
    claims = load_audit_claims(str(path))
    assert [c["tx_hash"] for c in claims] == ["0xaaa", "0xbbb"]


def test_load_audit_claims_missing_file_returns_empty(tmp_path):
    assert load_audit_claims(str(tmp_path / "nope.jsonl")) == []


# ── reconcile_claim ───────────────────────────────────────────
def test_repay_reconciles_against_real_log():
    node = FakeNode(_receipt([REAL_REPAY_LOG]))
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "MATCH"
    assert result.observed_event == "Repay"
    assert result.observed_amount == pytest.approx(13.23)
    assert _check(result, "wallet_matches")["passed"] is True


def test_borrow_reconciles_against_real_log():
    node = FakeNode(_receipt([REAL_BORROW_LOG]))
    result = reconcile_claim(_borrow_claim(), node)
    assert result.status == "MATCH"
    assert result.observed_event == "Borrow"
    assert result.observed_amount == pytest.approx(6.69)


def test_reverted_transaction_is_reported_as_reverted():
    """KeeperHub 说成功了但链上是 revert —— 这是最该抓出来的情形。"""
    node = FakeNode(_receipt([REAL_REPAY_LOG], status="0x0"))
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "REVERTED"
    assert _check(result, "tx_success")["passed"] is False
    assert "reverted" in _check(result, "tx_success")["detail"]


def test_missing_transaction_is_reported_as_not_found():
    """KeeperHub 报了执行, 但链上根本没有这笔交易。"""
    node = FakeNode(receipt=None)
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "NOT_FOUND"
    assert "no record" in _check(result, "receipt_available")["detail"]


def test_rpc_failure_is_surfaced_not_swallowed():
    node = FakeNode(error="connection refused")
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "ERROR"
    assert result.error == "connection refused"


def test_wallet_mismatch_fails():
    """事件属于别人的仓位 —— 中继执行下这是唯一的归属证据。"""
    stranger_log = dict(REAL_REPAY_LOG)
    stranger_log = json.loads(json.dumps(REAL_REPAY_LOG))
    stranger_log["topics"] = [
        _KNOWN_TOPIC0["Repay(address,address,address,uint256,bool)"],
        _word_address(USDC),
        _word_address("0x0000000000000000000000000000000000000abc"),  # user
        _word_address("0x0000000000000000000000000000000000000abc"),  # repayer
    ]
    node = FakeNode(_receipt([stranger_log]))
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "MISMATCH"
    assert _check(result, "wallet_matches")["passed"] is False


def test_event_type_mismatch_fails():
    """agent 声称是还债, 链上却是借款。"""
    node = FakeNode(_receipt([REAL_BORROW_LOG]))
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "MISMATCH"
    detail = _check(result, "event_matches_action")
    assert detail["passed"] is False
    assert "Repay" in detail["detail"] and "Borrow" in detail["detail"]


def test_amount_drift_beyond_tolerance_fails():
    node = FakeNode(_receipt([REAL_REPAY_LOG]))
    # 声称还了 100 USDC, 链上实际只有 13.23
    result = reconcile_claim(_repay_claim(amount=100.0), node)
    assert result.status == "MISMATCH"
    assert _check(result, "amount_matches")["passed"] is False


def test_amount_within_tolerance_passes():
    node = FakeNode(_receipt([REAL_REPAY_LOG]))
    # 13.23 vs 13.24: 相对偏差 0.076%, 在 1% 容差内
    result = reconcile_claim(_repay_claim(amount=13.24), node)
    assert _check(result, "amount_matches")["passed"] is True


def test_no_recognised_event_fails():
    node = FakeNode(_receipt([{"address": POOL, "topics": ["0x" + "22" * 32], "data": "0x"}]))
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "MISMATCH"
    assert _check(result, "event_emitted")["passed"] is False


def test_relayed_topology_is_recorded_not_punished():
    """中继执行下 tx 层的 from/to 必然不是我们的钱包, 不能据此判失败。

    第一版就是拿 from/to 硬比, 把两笔真实交易全判成了 MISMATCH。
    """
    node = FakeNode(_receipt([REAL_BORROW_LOG]))
    result = reconcile_claim(_borrow_claim(), node)
    topology = _check(result, "execution_topology")
    assert topology is not None
    assert topology["passed"] is True
    assert "relayed" in topology["detail"]
    # 关键: 归属由事件证明, 而不是 tx 字段
    assert _check(result, "wallet_matches")["passed"] is True


def test_direct_execution_also_reconciles():
    """钱包直连 Pool（非中继）时也应能通过。"""
    node = FakeNode(
        _receipt([REAL_REPAY_LOG], from_addr=WALLET, to_addr=POOL)
    )
    result = reconcile_claim(_repay_claim(), node)
    assert result.status == "MATCH"


def test_expected_event_mapping():
    assert EXPECTED_EVENT["PROTECT"] == "Repay"
    assert EXPECTED_EVENT["REBALANCE"] == "Borrow"


# ── reconcile_all / 报告 ──────────────────────────────────────
def test_reconcile_all_accepts_raw_audit_records(tmp_path):
    """传原始审计记录（而不是已 parse 的）时, action 不能丢。"""
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "PROTECT",
                "executed": True,
                "tx_hash": "0xaaa",
                "note": "repayed 13.23 USD USDC on Aave V3",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    claims = load_audit_claims(str(path))
    results = reconcile_all(claims, FakeNode(_receipt([REAL_REPAY_LOG])))
    assert len(results) == 1
    assert results[0].claimed_action == "PROTECT"
    assert results[0].status == "MATCH"


def test_format_report_all_match():
    node = FakeNode(_receipt([REAL_REPAY_LOG]))
    report = format_report([reconcile_claim(_repay_claim(), node)])
    assert "1/1 claims independently verified" in report
    assert "confirmed by an unrelated node" in report


def test_format_report_with_mismatch():
    node = FakeNode(_receipt([REAL_REPAY_LOG], status="0x0"))
    report = format_report([reconcile_claim(_repay_claim(), node)])
    assert "0/1 claims independently verified" in report
    assert "Discrepancies found" in report


def test_format_report_empty():
    assert "No executed transactions" in format_report([])
