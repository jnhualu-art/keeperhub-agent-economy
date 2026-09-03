#!/usr/bin/env python3
"""
OpenZeppelin Monitor 自定义过滤脚本 —— 只放行与受监控钱包相关的 Aave 事件。

调用约定（openzeppelin-monitor）:
  * Monitor 把 monitor match data 以 JSON 形式送到本脚本的 stdin
  * 退出码 0  -> 该 match 通过过滤, 继续触发后续 trigger
  * 退出码非 0 -> 丢弃该 match
  * 脚本超时被 kill 时, Monitor 默认放行（所以逻辑必须快, 别在里头发网络请求）
  * 修改本脚本后必须重启 Monitor, 脚本是启动时加载的

为什么需要它:
  match_conditions 只能按"合约地址 + 事件签名"匹配。Aave Pool 是全市场共用的,
  每天有无穷多笔别人的借还款。这一层把范围收窄到"我们自己的钱包", 否则
  webhook 会被无关交易淹没。

自检（不跑 Monitor 也能验证逻辑）:
    python3 keeperhub_execution_filter.py --selftest
"""

from __future__ import annotations

import json
import os
import sys

# 受监控的钱包。部署时可设环境变量覆盖, 默认取本项目的 Sepolia 钱包。
WATCHED_ADDRESS = os.getenv(
    "MONITOR_WATCHED_ADDRESS",
    "0x1573C3d151200922375bC48012BB1f232B2cF531",
).lower()

# 事件中代表"这笔交易属于谁"的参数名。
# 不同事件字段不同: Borrow/Supply 用 user + onBehalfOf, Withdraw 用 user + to,
# Repay 用 user + repayer。任一命中即视为相关。
_OWNERSHIP_FIELDS = ("user", "onBehalfOf", "to", "repayer")


def _as_list(value) -> list:
    """MonitorMatch 里同一字段可能是单个对象也可能是数组, 统一成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _iter_events(match: dict):
    """从 MonitorMatch 的各个可能位置把事件捞出来。

    openzeppelin-monitor 对 EVM 的 MonitorMatch 结构在不同版本间字段名略有出入,
    这里把已知的几个位置都找一遍, 而不是赌某一个字段名。
    """
    # 最常见的两处: matched_on.events 与顶层 events
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

    # 某些版本把解码后的参数单独放在 matched_on_args
    for key in ("matched_on_args", "matched_events_args"):
        container = match.get(key)
        if isinstance(container, dict):
            for ev in _as_list(container.get("events")):
                yield ev
        else:
            for ev in _as_list(container):
                yield ev


def _event_args(event) -> dict:
    """取出事件参数。可能挂在 args / decoded / params 下, 也可能事件本身就是参数字典。"""
    if not isinstance(event, dict):
        return {}
    for key in ("args", "decoded", "params", "values"):
        candidate = event.get(key)
        if isinstance(candidate, dict):
            return candidate
    # 没有专门的 args 容器: 事件字典自己就是 {name: value} 形式
    skipped = {"signature", "name", "event_name", "address", "logIndex", "topics", "data"}
    return {k: v for k, v in event.items() if k not in skipped}


def matches_watched_address(match: dict, watched: str | None = None) -> bool:
    """该 match 是否涉及受监控钱包。"""
    watched = (watched or WATCHED_ADDRESS).lower()
    for event in _iter_events(match):
        args = _event_args(event)
        for field in _OWNERSHIP_FIELDS:
            value = args.get(field)
            if isinstance(value, str) and value.lower() == watched:
                return True
    return False


def _selftest() -> int:
    """用构造的 payload 验证过滤逻辑, 不需要真的跑 Monitor。"""
    watched = "0x1573c3d151200922375bc48012bb1f232b2cf531"
    cases = [
        (
            "钱包自己的 Borrow 事件",
            {
                "matched_on": {
                    "events": [
                        {
                            "signature": "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
                            "args": {
                                "reserve": "0x94a9d9ac8a22534e3faca9f4e7f2e2cf85d5e4c8",
                                "user": watched,
                                "onBehalfOf": watched,
                                "amount": "6690000",
                                "interestRateMode": 2,
                            },
                        }
                    ]
                }
            },
            True,
        ),
        (
            "别人的 Borrow 事件 -> 丢弃",
            {
                "matched_on": {
                    "events": [
                        {
                            "signature": "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
                            "args": {
                                "user": "0x0000000000000000000000000000000000000abc",
                                "onBehalfOf": "0x0000000000000000000000000000000000000abc",
                                "amount": "1000000",
                            },
                        }
                    ]
                }
            },
            False,
        ),
        (
            "Repay 由第三方代还, 但 user 是我们 -> 放行",
            {
                "matched_on": {
                    "events": [
                        {
                            "signature": "Repay(address,address,address,uint256,bool)",
                            "args": {
                                "user": watched,
                                "repayer": "0x0000000000000000000000000000000000000def",
                                "amount": "13230000",
                            },
                        }
                    ]
                }
            },
            True,
        ),
        (
            "地址大小写不同也应命中",
            {
                "matched_on": {
                    "events": [
                        {
                            "signature": "Supply(address,address,address,uint256,uint16)",
                            "args": {"user": watched.upper().replace("0X", "0x")},
                        }
                    ]
                }
            },
            True,
        ),
        (
            "空 payload -> 丢弃",
            {},
            False,
        ),
    ]

    failures = 0
    for name, payload, expected in cases:
        got = matches_watched_address(payload, watched)
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            failures += 1
        print(f"[{status}] {name}")

    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    try:
        raw = sys.stdin.read()
        match = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        # 读不懂的 payload 一律丢弃, 宁可漏报也不要把垃圾推给下游
        return 1

    return 0 if matches_watched_address(match) else 1


if __name__ == "__main__":
    sys.exit(main())
