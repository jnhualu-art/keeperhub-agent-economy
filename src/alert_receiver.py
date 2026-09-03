"""
OpenZeppelin Monitor webhook 接收端 —— 独立链上观测的入口。

推送路径: 自托管的 openzeppelin-monitor 在链上看到匹配事件 -> 带 HMAC 签名 POST
到这里 -> 本模块验签、归一化、落盘。

验签规则（来自官方 Monitor 文档）:
    1. Monitor 生成毫秒级时间戳
    2. signature = HMAC-SHA256(secret, payload_string + timestamp_string)
    3. 通过请求头 X-Signature / X-Timestamp 发送

因此接收端必须拿**原始 body 字符串**去算签名。任何先 json.loads 再 dumps 的做法
都会因为键顺序 / 空格差异导致验签失败。

纯标准库实现, 不引入新依赖。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# ── 签名头 ────────────────────────────────────────────────────
SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"

# 防重放窗口: 超过这个时间差的请求直接丢弃, 避免有人录下旧请求重放
DEFAULT_TOLERANCE_MS = 300_000  # 5 分钟

# ── Sepolia 资产表 ────────────────────────────────────────────
# 链上事件给出的是 base units, 要还原成人类可读金额就得知道每个代币的 decimals。
# 地址来自 config.py 的 Aave Sepolia address book; 这里内置一份是为了让本模块
# 在测试环境里不依赖 config（config 在 import 时会快照环境变量）。
SEPOLIA_ASSETS: Dict[str, Dict[str, Any]] = {
    "0x94a9d9ac8a22534e3faca9f4e7f2e2cf85d5e4c8": {"symbol": "USDC", "decimals": 6},
    "0xc558dbdd856501fcd9aaf1e62eae57a9f0629a3c": {"symbol": "WETH", "decimals": 18},
    "0xff34b3d4aee8ddcd6f9afffb6fe49bd371b8a357": {"symbol": "DAI", "decimals": 18},
    "0xf8fb3713d459d7c1018bd0a49d19b4c44290ebe5": {"symbol": "LINK", "decimals": 18},
    "0x88541670e55cc00beefd87eb59edd1b7c511ac9a": {"symbol": "AAVE", "decimals": 18},
    "0x29f2d40b0605204364af54ec677bd022da425d03": {"symbol": "WBTC", "decimals": 8},
    "0xaa8e23fb1079ea71e0a56f48a2aa51851d8433d0": {"symbol": "USDT", "decimals": 6},
    "0xc4bf5cbdabe595361438f8c6a187bdc330539c60": {"symbol": "GHO", "decimals": 18},
}

# 需要盯的 Aave V3 Pool 事件 -> 是否被清算（严重级别）
CRITICAL_EVENTS = {"LiquidationCall"}


def _default_alert_path() -> str:
    """告警落盘路径, 默认 <repo>/logs/alerts.jsonl。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "logs", "alerts.jsonl")


@dataclass
class Alert:
    """归一化后的链上观测告警。"""

    tx_hash: str
    event: str
    severity: str
    asset: Optional[str] = None
    amount: Optional[float] = None
    amount_base: Optional[int] = None
    wallet: Optional[str] = None
    block_number: Optional[int] = None
    interest_rate_mode: Optional[int] = None
    monitor: Optional[str] = None
    received_at: float = field(default_factory=time.time)
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self, include_raw: bool = False) -> dict:
        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        return data


class AlertReceiverError(Exception):
    """验签失败 / payload 无法解析时抛出。"""


# ── 验签 ──────────────────────────────────────────────────────
def compute_signature(payload: str, timestamp: str, secret: str) -> str:
    """HMAC-SHA256(secret, payload + timestamp), 返回 hex。"""
    message = (payload + timestamp).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    payload: str,
    timestamp: str,
    signature: str,
    secret: str,
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
    now_ms: Optional[int] = None,
) -> bool:
    """校验 Monitor 送来的签名, 同时防重放。

    payload    必须是**原始请求体字符串**, 不能是重新序列化过的 JSON
    timestamp  毫秒级时间戳字符串
    signature  X-Signature 头的值
    """
    if not secret or not signature or not timestamp:
        return False

    try:
        ts_ms = int(timestamp)
    except (TypeError, ValueError):
        return False

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if abs(now_ms - ts_ms) > tolerance_ms:
        return False

    expected = compute_signature(payload, timestamp, secret)
    # compare_digest 抗时序攻击; 统一小写以容忍大小写差异
    return hmac.compare_digest(expected, signature.strip().lower())


# ── 解析 ──────────────────────────────────────────────────────
def _as_list(value) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _iter_events(match: dict) -> Iterable[dict]:
    """从 MonitorMatch 里捞出事件对象（不同版本字段名略有出入, 多个位置都找）。"""
    for key in ("matched_on", "match_data", "matched_events"):
        container = match.get(key)
        if isinstance(container, dict):
            for ev in _as_list(container.get("events")):
                yield ev
        else:
            for ev in _as_list(container):
                yield ev

    for ev in _as_list(match.get("events")):
        yield ev

    for key in ("matched_on_args", "matched_events_args"):
        container = match.get(key)
        if isinstance(container, dict):
            for ev in _as_list(container.get("events")):
                yield ev
        else:
            for ev in _as_list(container):
                yield ev


def _event_args(event) -> dict:
    if not isinstance(event, dict):
        return {}
    for key in ("args", "decoded", "params", "values"):
        candidate = event.get(key)
        if isinstance(candidate, dict):
            return candidate
    skipped = {"signature", "name", "event_name", "address", "logIndex", "topics", "data"}
    return {k: v for k, v in event.items() if k not in skipped}


def _event_name(event: dict, args: dict) -> Optional[str]:
    """事件名: 优先 signature, 其次是 name 字段。"""
    for key in ("signature", "event_name", "name"):
        value = event.get(key)
        if isinstance(value, str) and value:
            # signature 形如 "Borrow(address,address,...)", 取括号前的部分
            return value.split("(")[0].strip()
    return None


def _resolve_asset(args: dict, assets: Dict[str, Dict[str, Any]]):
    """从事件中找出资产地址 -> (symbol, decimals)。"""
    for key in ("reserve", "asset", "debtAsset", "collateralAsset"):
        addr = args.get(key)
        if isinstance(addr, str):
            info = assets.get(addr.lower())
            if info:
                return addr.lower(), info
            return addr.lower(), None
    return None, None


def _to_base_units(value) -> Optional[int]:
    """事件里的金额可能是 int / hex 字符串 / 十进制字符串。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def parse_monitor_match(
    match: dict,
    assets: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Alert]:
    """把 raw MonitorMatch 归一化成一到多条 Alert。"""
    assets = assets if assets is not None else SEPOLIA_ASSETS

    tx = match.get("transaction") or {}
    tx_hash = tx.get("hash") or match.get("transaction_hash") or match.get("hash") or ""
    block_number = tx.get("blockNumber") or match.get("block_number")
    if isinstance(block_number, str):
        block_number = _to_base_units(block_number)

    monitor_name = None
    monitor = match.get("monitor")
    if isinstance(monitor, dict):
        monitor_name = monitor.get("name")
    elif isinstance(monitor, str):
        monitor_name = monitor

    alerts: List[Alert] = []
    for event in _iter_events(match):
        args = _event_args(event)
        name = _event_name(event, args)
        if not name:
            continue

        asset_addr, asset_info = _resolve_asset(args, assets)
        amount_base = _to_base_units(
            args.get("amount") or args.get("debtToCover") or args.get("value")
        )
        amount = None
        if amount_base is not None and asset_info:
            amount = amount_base / (10 ** asset_info["decimals"])

        wallet = None
        for field_name in ("user", "onBehalfOf", "to", "repayer"):
            value = args.get(field_name)
            if isinstance(value, str):
                wallet = value
                break

        alerts.append(
            Alert(
                tx_hash=tx_hash,
                event=name,
                severity="critical" if name in CRITICAL_EVENTS else "info",
                asset=asset_info["symbol"] if asset_info else None,
                amount=amount,
                amount_base=amount_base,
                wallet=wallet,
                block_number=block_number,
                interest_rate_mode=_to_base_units(args.get("interestRateMode")),
                monitor=monitor_name,
                raw=event,
            )
        )

    return alerts


# ── 落盘 ──────────────────────────────────────────────────────
def append_alerts(alerts: Iterable[Alert], path: Optional[str] = None) -> int:
    """追加写入告警日志, 一行一条 JSON。返回写入条数。"""
    path = path or _default_alert_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    count = 0
    with open(path, "a", encoding="utf-8") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")
            count += 1
    return count


def handle_webhook(
    body: str,
    headers: Dict[str, str],
    secret: str,
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
    now_ms: Optional[int] = None,
    alert_path: Optional[str] = None,
) -> List[Alert]:
    """完整入口: 验签 -> 解析 -> 落盘。

    body    原始请求体字符串（必须原样传入, 否则验签必失败）
    headers 请求头字典（大小写不敏感）
    """
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    signature = headers.get(SIGNATURE_HEADER.lower(), "")
    timestamp = headers.get(TIMESTAMP_HEADER.lower(), "")

    if not verify_signature(body, timestamp, signature, secret, tolerance_ms, now_ms):
        raise AlertReceiverError("signature verification failed")

    try:
        match = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AlertReceiverError(f"invalid JSON payload: {exc}") from exc

    if not isinstance(match, dict):
        raise AlertReceiverError("payload must be a JSON object")

    alerts = parse_monitor_match(match)
    if alerts:
        append_alerts(alerts, alert_path)
    return alerts


def format_alert(alert: Alert) -> str:
    """人类可读的一行摘要, 用于日志和终端输出。"""
    parts = [f"[{alert.severity.upper()}]", alert.event]
    if alert.asset and alert.amount is not None:
        parts.append(f"{alert.amount:.6f} {alert.asset}")
    if alert.wallet:
        parts.append(f"wallet={alert.wallet[:10]}…")
    if alert.tx_hash:
        parts.append(f"tx={alert.tx_hash[:12]}…")
    if alert.interest_rate_mode is not None:
        # Aave V3: 1 = stable, 2 = variable
        mode = {1: "stable", 2: "variable"}.get(alert.interest_rate_mode, "?")
        parts.append(f"rate={mode}")
    return "  ".join(parts)
