"""
独立对账 —— 拿第三方节点的链上事实, 核对 KeeperHub 自己的执行报告。

这一层是整个项目 trustlessness 主张的落点。

问题:
    logs/audit.jsonl 记的是 KeeperHub **返回给我们**的执行报告。它说
    "executed: true" 我们就信了。但这份报告是它自己出的——如果它漏报、
    报错、或者把 revert 说成成功, 我们的审计日志会跟着一起错, 而且
    我们没有任何机制能发现。

做法:
    对每一条 KeeperHub 声称执行过的交易, 去一个**与 KeeperHub 无关**的
    公共节点把回执拉回来, 逐项核对:
      • 这笔交易在链上真的存在吗
      • 它成功了吗 (status == 1)
      • 是不是从我们的钱包发出、打到 Aave Pool
      • Pool 实际发出的事件类型和动作对不对 (PROTECT -> Repay,
        REBALANCE -> Borrow)
      • 链上实际金额和 agent 声称的金额是否在一个容差内

两边都说得通才算"验证过"。只有 KeeperHub 一方说, 那叫"报告过"。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import config as app_config
from evm import EvmClient, RpcError, decode_aave_log, event_wallet

# KeeperHub 的动作类型 -> Aave Pool 应该发出的事件
EXPECTED_EVENT: Dict[str, str] = {
    "PROTECT": "Repay",      # 防守: 还债拉高健康因子
    "REBALANCE": "Borrow",   # 资本效率: 借出闲置额度
    "MIGRATE": "Supply",
    "ENTER": "Supply",
}

# 金额对账容差。链上有取整, 用相对 1% 与绝对 0.01 USD 取大者。
REL_TOLERANCE = 0.01
ABS_TOLERANCE = 0.01

# note 形如: "borrowed 6.69 USD USDC on Aave V3, ..." / "repayed 13.23 USD USDC ..."
_NOTE_RE = re.compile(
    r"(?P<verb>borrowed|repayed|supplied|withdrew)\s+"
    r"(?P<amount>\d+(?:\.\d+)?)\s*USD\s+"
    r"(?P<asset>[A-Za-z0-9]+)",
    re.IGNORECASE,
)


@dataclass
class ReconcileResult:
    """单笔交易的对账结论。"""

    tx_hash: str
    claimed_action: str
    claimed_amount: Optional[float]
    claimed_asset: Optional[str]
    status: str                      # MATCH / MISMATCH / NOT_FOUND / REVERTED / ERROR
    expected_event: Optional[str] = None
    observed_event: Optional[str] = None
    observed_amount: Optional[float] = None
    checks: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "MATCH"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        return data


def _add_check(result: ReconcileResult, name: str, passed: bool, detail: str) -> None:
    result.checks.append({"check": name, "passed": passed, "detail": detail})


def load_audit_claims(path: Optional[str] = None) -> List[dict]:
    """从审计日志里取出所有 KeeperHub 声称执行过的记录。"""
    path = path or _default_audit_path()
    claims: List[dict] = []
    if not os.path.exists(path):
        return claims

    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 只核对被声称真正广播过的, dry_run 与跳过的不算
            if record.get("executed") is True and record.get("tx_hash"):
                claims.append(record)
    return claims


def _default_audit_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "logs", "audit.jsonl")


def parse_claim(record: dict) -> Dict[str, Any]:
    """从审计记录里解析出 KeeperHub 声称的内容。

    幂等: 传入已经 parse 过的 claim 时原样返回, 避免调用方重复 parse 导致
    action 丢失（原始审计记录用 "type" 存动作, parse 后改叫 "action"）。
    """
    if "type" not in record and "action" in record:
        return dict(record)

    note = record.get("note") or ""
    match = _NOTE_RE.search(note)

    verb_map = {
        "borrowed": "Borrow",
        "repayed": "Repay",
        "supplied": "Supply",
        "withdrew": "Withdraw",
    }

    claimed_event = None
    amount = None
    asset = None
    if match:
        amount = float(match.group("amount"))
        asset = match.group("asset").upper()
        claimed_event = verb_map.get(match.group("verb").lower())

    return {
        "tx_hash": record["tx_hash"],
        "action": record.get("type") or "",
        "amount": amount,
        "asset": asset,
        "claimed_event": claimed_event,
        "note": note,
    }


def reconcile_claim(
    claim: Dict[str, Any],
    client: Optional[EvmClient] = None,
    pool_address: Optional[str] = None,
    wallet_address: Optional[str] = None,
) -> ReconcileResult:
    """核对单笔声称执行过的交易。"""
    client = client or EvmClient()
    pool = (pool_address or app_config.AAVE_POOL).lower()
    wallet = (wallet_address or app_config.WALLET_ADDRESS).lower()

    action = claim.get("action") or ""
    expected_event = EXPECTED_EVENT.get(action, claim.get("claimed_event"))

    result = ReconcileResult(
        tx_hash=claim["tx_hash"],
        claimed_action=action,
        claimed_amount=claim.get("amount"),
        claimed_asset=claim.get("asset"),
        status="MISMATCH",
        expected_event=expected_event,
    )

    # ① 链上能查到这笔交易吗
    try:
        receipt = client.get_receipt(claim["tx_hash"])
    except RpcError as exc:
        result.status = "ERROR"
        result.error = str(exc)
        _add_check(result, "receipt_available", False, str(exc))
        return result

    if receipt is None:
        result.status = "NOT_FOUND"
        _add_check(
            result,
            "receipt_available",
            False,
            "transaction not found on the independent node — KeeperHub "
            "reported execution but the chain has no record of it",
        )
        return result
    _add_check(result, "receipt_available", True, "found on independent node")

    # ② 这笔交易成功了吗
    status = receipt.get("status")
    status_int = int(status, 16) if isinstance(status, str) else status
    if status_int != 1:
        result.status = "REVERTED"
        _add_check(
            result,
            "tx_success",
            False,
            f"status={status} — KeeperHub reported success but the transaction reverted",
        )
        return result
    _add_check(result, "tx_success", True, "status=1")

    # ③ 执行拓扑 —— 只记录, 不作为判据
    #
    # KeeperHub 是 gas 代付的中继执行: 链上这笔交易里 from 是 relayer 的 EOA、
    # to 是 KeeperHub 的 router 合约, 而不是我们的钱包直连 Aave Pool。因此
    # 拿交易层的 from/to 去判断"这笔交易是不是我们的"必然误判——第一版就是
    # 这么写的, 把两笔真实交易全判成了 MISMATCH。
    #
    # 真正的归属证据在 Pool 发出的事件里: 那个 user / onBehalfOf 字段。
    # 见下面的 wallet_matches 检查。
    to_addr = (receipt.get("to") or "").lower()
    from_addr = (receipt.get("from") or "").lower()
    _add_check(
        result,
        "execution_topology",
        True,
        f"from={from_addr[:12]}… to={to_addr[:12]}… — relayed, gas sponsored by "
        "KeeperHub; ownership is proven by the Pool event below, not tx fields",
    )

    # ④ Pool 实际发出了什么事件
    events = []
    for log in receipt.get("logs") or []:
        decoded = decode_aave_log(log)
        if decoded and decoded.get("address") == pool:
            events.append(decoded)

    if not events:
        _add_check(
            result,
            "event_emitted",
            False,
            "no recognised Aave Pool event in the receipt",
        )
        return result

    observed = events[0]
    result.observed_event = observed.get("event")
    _add_check(
        result,
        "event_emitted",
        True,
        f"Pool emitted {result.observed_event}",
    )

    if expected_event and result.observed_event != expected_event:
        _add_check(
            result,
            "event_matches_action",
            False,
            f"agent claimed {action} (expects {expected_event}) but chain shows "
            f"{result.observed_event}",
        )
    else:
        _add_check(result, "event_matches_action", True, f"{action} -> {result.observed_event}")

    # ⑤ 这笔事件到底作用在谁的仓位上
    # 中继执行下, 这是唯一能证明"这笔交易服务于我们的钱包"的证据
    subject = (event_wallet(observed) or "").lower()
    if not subject:
        _add_check(
            result, "wallet_matches", False, "no user/onBehalfOf field decoded from the event"
        )
    elif subject != wallet:
        _add_check(
            result,
            "wallet_matches",
            False,
            f"event is for {subject}, not our monitored wallet {wallet}",
        )
    else:
        _add_check(result, "wallet_matches", True, f"event subject = {wallet[:12]}…")

    # ⑥ 金额对得上吗
    amount_base = observed.get("amount_base")
    decimals = None
    if claim.get("asset"):
        decimals = app_config.TOKENS.get(claim["asset"], {}).get("decimals")
    if amount_base is not None and decimals is not None:
        result.observed_amount = amount_base / (10**decimals)
        claimed = claim.get("amount")
        if claimed is None:
            _add_check(result, "amount_matches", False, "no amount found in the audit note")
        else:
            tolerance = max(claimed * REL_TOLERANCE, ABS_TOLERANCE)
            delta = abs(result.observed_amount - claimed)
            if delta <= tolerance:
                _add_check(
                    result,
                    "amount_matches",
                    True,
                    f"claimed {claimed} vs on-chain {result.observed_amount:.6f} "
                    f"(delta {delta:.6f} <= {tolerance:.4f})",
                )
            else:
                _add_check(
                    result,
                    "amount_matches",
                    False,
                    f"claimed {claimed} vs on-chain {result.observed_amount:.6f} "
                    f"(delta {delta:.6f} > {tolerance:.4f})",
                )
    else:
        _add_check(
            result,
            "amount_matches",
            False,
            f"cannot decode amount (base={amount_base}, decimals={decimals})",
        )

    # 结论: 所有检查项都通过才算对上
    result.status = "MATCH" if all(c["passed"] for c in result.checks) else "MISMATCH"
    return result


def reconcile_all(
    claims: Optional[Iterable[dict]] = None,
    client: Optional[EvmClient] = None,
    audit_path: Optional[str] = None,
) -> List[ReconcileResult]:
    """把审计日志里所有声称执行过的交易都核一遍。"""
    claims = list(claims) if claims is not None else load_audit_claims(audit_path)
    return [reconcile_claim(parse_claim(c), client) for c in claims]


def format_result(result: ReconcileResult) -> str:
    """终端用的一行摘要。"""
    icon = {"MATCH": "OK  ", "MISMATCH": "DIFF", "NOT_FOUND": "MISS",
            "REVERTED": "RVRT", "ERROR": "ERR "}.get(result.status, "????")

    amount_txt = "?"
    if result.claimed_amount is not None:
        amount_txt = f"{result.claimed_amount:.2f}"
    if result.observed_amount is not None:
        amount_txt += f" -> {result.observed_amount:.6f}"

    line = (
        f"[{icon}] {result.tx_hash[:14]}…  {result.claimed_action:10} "
        f"{amount_txt} {result.claimed_asset or ''}"
    )
    if result.expected_event and result.observed_event:
        line += f"  ({result.expected_event}->{result.observed_event})"
    return line


def format_report(results: Iterable[ReconcileResult]) -> str:
    """完整报告。"""
    results = list(results)
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("INDEPENDENT RECONCILIATION — KeeperHub's claims vs third-party chain data")
    lines.append("=" * 78)
    lines.append("")

    if not results:
        lines.append("No executed transactions found in the audit log.")
        lines.append("")
        return "\n".join(lines)

    for result in results:
        lines.append(format_result(result))
        for check in result.checks:
            mark = "  +" if check["passed"] else "  !"
            lines.append(f"{mark} {check['check']}: {check['detail']}")
        lines.append("")

    matched = sum(1 for r in results if r.ok)
    lines.append("-" * 78)
    lines.append(f"{matched}/{len(results)} claims independently verified")

    if matched == len(results):
        lines.append(
            "Every execution KeeperHub reported is confirmed by an unrelated node:"
            " same transaction, same event, same amount."
        )
    else:
        lines.append(
            "Discrepancies found. Do not trust the audit log alone — the chain "
            "is the source of truth."
        )
    lines.append("")
    return "\n".join(lines)
