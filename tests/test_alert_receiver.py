"""alert_receiver.py 测试 —— webhook 验签与告警归一化。

签名规则（OpenZeppelin Monitor 文档）:
    signature = HMAC-SHA256(secret, payload_string + timestamp_string)
通过 X-Signature / X-Timestamp 头传递。payload 必须是原始 body 字符串。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from alert_receiver import (
    Alert,
    AlertReceiverError,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    append_alerts,
    compute_signature,
    format_alert,
    handle_webhook,
    parse_monitor_match,
    verify_signature,
)

SECRET = "test-secret-value"
NOW_MS = 1_700_000_000_000

USDC = "0x94a9d9ac8a22534e3faca9f4e7f2e2cf85d5e4c8"
WALLET = "0x1573c3d151200922375bc48012bb1f232b2cf531"


def _sign(body: str, timestamp_ms: int, secret: str = SECRET) -> dict:
    return {
        SIGNATURE_HEADER: compute_signature(body, str(timestamp_ms), secret),
        TIMESTAMP_HEADER: str(timestamp_ms),
    }


def _borrow_match() -> dict:
    """模拟 Monitor 在 raw 模式下 POST 过来的完整 MonitorMatch。"""
    return {
        "monitor": {"name": "KeeperHub Agent Execution - Aave V3 Sepolia"},
        "network": "ethereum_sepolia",
        "transaction": {"hash": "0x" + "ab" * 32, "blockNumber": "0xb1c2d3"},
        "matched_on": {
            "events": [
                {
                    "signature": "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
                    "args": {
                        "reserve": USDC,
                        "user": WALLET,
                        "onBehalfOf": WALLET,
                        "amount": "6690000",
                        "interestRateMode": 2,
                    },
                }
            ]
        },
    }


# ── 签名 ──────────────────────────────────────────────────────
def test_compute_signature_matches_reference_implementation():
    """跟 hmac 的标准用法对一遍, 确认没把 payload/timestamp 顺序搞反。"""
    body, ts = '{"a":1}', "1700000000000"
    expected = hmac.new(
        SECRET.encode(), (body + ts).encode(), hashlib.sha256
    ).hexdigest()
    assert compute_signature(body, ts, SECRET) == expected


def test_valid_signature_passes():
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS)
    assert (
        verify_signature(
            body, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], SECRET, now_ms=NOW_MS
        )
        is True
    )


def test_wrong_signature_fails():
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS, secret="some-other-secret")
    assert (
        verify_signature(
            body, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], SECRET, now_ms=NOW_MS
        )
        is False
    )


def test_tampered_payload_fails():
    """body 被改过一个字节, 签名就应当对不上。"""
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS)
    tampered = body.replace("6690000", "9990000")
    assert (
        verify_signature(
            tampered, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], SECRET, now_ms=NOW_MS
        )
        is False
    )


def test_stale_timestamp_is_rejected():
    """防重放: 五分钟前的请求必须拒掉。"""
    body = json.dumps(_borrow_match())
    old_ms = NOW_MS - 600_000  # 10 分钟前
    headers = _sign(body, old_ms)
    assert (
        verify_signature(
            body, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], SECRET, now_ms=NOW_MS
        )
        is False
    )


def test_future_timestamp_beyond_tolerance_is_rejected():
    body = json.dumps(_borrow_match())
    future_ms = NOW_MS + 600_000
    headers = _sign(body, future_ms)
    assert (
        verify_signature(
            body, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], SECRET, now_ms=NOW_MS
        )
        is False
    )


def test_timestamp_within_tolerance_passes():
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS - 60_000)  # 1 分钟前
    assert (
        verify_signature(
            body, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], SECRET, now_ms=NOW_MS
        )
        is True
    )


def test_missing_or_malformed_credentials_fail():
    body = json.dumps(_borrow_match())
    assert verify_signature(body, str(NOW_MS), "abc123", "") is False
    assert verify_signature(body, str(NOW_MS), "", SECRET) is False
    assert verify_signature(body, "", "abc123", SECRET, now_ms=NOW_MS) is False
    # 时间戳不是数字
    assert verify_signature(body, "not-a-number", "abc123", SECRET, now_ms=NOW_MS) is False


def test_signature_comparison_is_case_insensitive():
    """有的客户端会返回大写 hex。"""
    body = json.dumps(_borrow_match())
    sig = compute_signature(body, str(NOW_MS), SECRET).upper()
    assert verify_signature(body, str(NOW_MS), sig, SECRET, now_ms=NOW_MS) is True


# ── 解析 ──────────────────────────────────────────────────────
def test_parse_monitor_match_normalizes_borrow():
    alerts = parse_monitor_match(_borrow_match())
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.event == "Borrow"
    assert alert.asset == "USDC"
    assert alert.amount == pytest.approx(6.69)
    assert alert.amount_base == 6_690_000
    assert alert.wallet == WALLET
    assert alert.interest_rate_mode == 2
    assert alert.severity == "info"
    assert alert.monitor == "KeeperHub Agent Execution - Aave V3 Sepolia"


def test_parse_monitor_match_decodes_block_number_from_hex():
    alerts = parse_monitor_match(_borrow_match())
    assert alerts[0].block_number == 0xB1C2D3


def test_liquidation_call_is_critical():
    """被清算说明兜底防线失守, 必须是最严重级别。"""
    match = {
        "transaction": {"hash": "0x" + "cd" * 32},
        "matched_on": {
            "events": [
                {
                    "signature": "LiquidationCall(address,address,address,uint256,uint256,address,bool)",
                    "args": {
                        "collateralAsset": "0xc558dbdd856501fcd9aaf1e62eae57a9f0629a3c",
                        "debtAsset": USDC,
                        "user": WALLET,
                        "debtToCover": "50000000",
                    },
                }
            ]
        },
    }
    alerts = parse_monitor_match(match)
    assert len(alerts) == 1
    assert alerts[0].event == "LiquidationCall"
    assert alerts[0].severity == "critical"
    assert alerts[0].amount == pytest.approx(50.0)


def test_parse_handles_hex_amounts():
    match = {
        "transaction": {"hash": "0x" + "ee" * 32},
        "matched_on": {
            "events": [
                {
                    "signature": "Repay(address,address,address,uint256,bool)",
                    "args": {"reserve": USDC, "user": WALLET, "amount": "0xc9dfb0"},
                }
            ]
        },
    }
    alerts = parse_monitor_match(match)
    assert alerts[0].amount_base == 13_230_000
    assert alerts[0].amount == pytest.approx(13.23)


def test_parse_handles_args_without_container():
    """MonitorMatch 的某些变体把参数直接摊平在事件对象上。"""
    match = {
        "transaction": {"hash": "0x" + "ff" * 32},
        "events": [
            {
                "signature": "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
                "reserve": USDC,
                "user": WALLET,
                "amount": 6690000,
            }
        ],
    }
    alerts = parse_monitor_match(match)
    assert len(alerts) == 1
    assert alerts[0].amount == pytest.approx(6.69)


def test_parse_unknown_asset_still_records_base_amount():
    match = {
        "transaction": {"hash": "0x" + "11" * 32},
        "events": [
            {
                "signature": "Supply(address,address,address,uint256,uint16)",
                "args": {"reserve": "0x" + "99" * 20, "user": WALLET, "amount": "1000"},
            }
        ],
    }
    alerts = parse_monitor_match(match)
    assert alerts[0].asset is None      # 资产表外的代币, 认不出符号
    assert alerts[0].amount_base == 1000
    assert alerts[0].amount is None     # 不知道 decimals 就不该猜金额


def test_parse_empty_match_returns_no_alerts():
    assert parse_monitor_match({}) == []


def test_parse_ignores_events_without_a_name():
    match = {"transaction": {"hash": "0xabc"}, "events": [{"args": {"amount": "1"}}]}
    assert parse_monitor_match(match) == []


# ── 落盘与端到端 ──────────────────────────────────────────────
def test_append_alerts_writes_jsonl(tmp_path):
    alerts = parse_monitor_match(_borrow_match())
    path = tmp_path / "alerts.jsonl"
    assert append_alerts(alerts, str(path)) == 1

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "Borrow"
    assert record["amount"] == pytest.approx(6.69)
    assert "raw" not in record  # 落盘不带原始负载, 避免日志膨胀


def test_append_alerts_creates_directory(tmp_path):
    path = tmp_path / "nested" / "deep" / "alerts.jsonl"
    append_alerts(parse_monitor_match(_borrow_match()), str(path))
    assert path.exists()


def test_handle_webhook_end_to_end(tmp_path):
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS)
    path = tmp_path / "alerts.jsonl"

    alerts = handle_webhook(
        body, headers, SECRET, now_ms=NOW_MS, alert_path=str(path)
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Borrow"
    assert path.exists()


def test_handle_webhook_rejects_bad_signature(tmp_path):
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS, secret="wrong")
    with pytest.raises(AlertReceiverError, match="signature verification failed"):
        handle_webhook(body, headers, SECRET, now_ms=NOW_MS, alert_path=str(tmp_path / "a.jsonl"))


def test_handle_webhook_rejects_invalid_json(tmp_path):
    body = "not json at all"
    headers = _sign(body, NOW_MS)
    with pytest.raises(AlertReceiverError, match="invalid JSON"):
        handle_webhook(body, headers, SECRET, now_ms=NOW_MS, alert_path=str(tmp_path / "a.jsonl"))


def test_handle_webhook_rejects_non_object_payload(tmp_path):
    body = "[1, 2, 3]"
    headers = _sign(body, NOW_MS)
    with pytest.raises(AlertReceiverError, match="must be a JSON object"):
        handle_webhook(body, headers, SECRET, now_ms=NOW_MS, alert_path=str(tmp_path / "a.jsonl"))


def test_handle_webhook_is_header_case_insensitive(tmp_path):
    body = json.dumps(_borrow_match())
    timestamp = str(NOW_MS)
    headers = {
        "x-signature": compute_signature(body, timestamp, SECRET),
        "x-timestamp": timestamp,
    }
    alerts = handle_webhook(
        body, headers, SECRET, now_ms=NOW_MS, alert_path=str(tmp_path / "a.jsonl")
    )
    assert len(alerts) == 1


def test_rejected_webhook_writes_nothing(tmp_path):
    """验签失败时绝不能落盘, 否则伪造的告警会污染日志。"""
    path = tmp_path / "alerts.jsonl"
    body = json.dumps(_borrow_match())
    headers = _sign(body, NOW_MS, secret="attacker")
    with pytest.raises(AlertReceiverError):
        handle_webhook(body, headers, SECRET, now_ms=NOW_MS, alert_path=str(path))
    assert not path.exists()


# ── 格式化 ────────────────────────────────────────────────────
def test_format_alert_includes_key_fields():
    alert = Alert(
        tx_hash="0x" + "ab" * 32,
        event="Borrow",
        severity="info",
        asset="USDC",
        amount=6.69,
        wallet=WALLET,
        interest_rate_mode=2,
    )
    text = format_alert(alert)
    assert "BORROW" in text.upper()
    assert "6.690000 USDC" in text
    assert "variable" in text


def test_format_alert_handles_stable_rate():
    alert = Alert(tx_hash="0xabc", event="Borrow", severity="info", interest_rate_mode=1)
    assert "stable" in format_alert(alert)


def test_alert_to_dict_omits_raw_by_default():
    alert = Alert(tx_hash="0xabc", event="Repay", severity="info", raw={"x": 1})
    assert "raw" not in alert.to_dict()
    assert alert.to_dict(include_raw=True)["raw"] == {"x": 1}


def test_alert_received_at_is_populated():
    before = time.time()
    alert = Alert(tx_hash="0xabc", event="Repay", severity="info")
    assert before <= alert.received_at <= time.time()
