"""erc8004.py 审计探针 —— 每个怀疑都跑出证据, 不靠推理下结论。

erc8004 是索引器, 不碰私钥也不发交易, 但它是**唯一消费链上不可信字符串
的模块**: agentURI 由任意注册者写入链上, 本模块会照着去抓、去解析。
威胁模型: 攻击者注册一个 agent, 把 URI 指向他想让我们访问的地址。

用法: python scripts/audit_erc8004.py
"""

from __future__ import annotations

import asyncio
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

results: list[tuple[bool, str, str]] = []


def probe(name: str, fn):
    try:
        hit, evidence = fn()
    except Exception as exc:
        hit, evidence = True, f"探针抛出异常 {type(exc).__name__}: {exc}"
    tag = "[命中]" if hit else "[安全]"
    results.append((hit, name, evidence))
    print(f"{tag} {name}")
    print(f"       {evidence}")


# ── 1. SSRF: 链上 URI 指向内网 / 云元数据 ────────────────────────
def _unsafe_targets():
    """应当被拒绝抓取的目标清单。"""
    return [
        "http://169.254.169.254/latest/meta-data/",  # 云实例元数据
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]:22/",
        "file:///etc/passwd",
        "ftp://evil.com/x",
        "data:text/plain,hi",
        "gopher://x/",
    ]


def p_ssrf_internal_uri():
    """agentURI 是链上任意写入的字符串 —— 典型 SSRF 面。

    云主机的 169.254.169.254 是实例元数据服务(凭据泄露重灾区), 内网地址
    同理。攻击者只需花一点 gas 注册一个 agent, 就能让索引器替他发请求。
    """
    from erc8004 import is_safe_http_url

    leaked = [u for u in _unsafe_targets() if is_safe_http_url(u)[0]]
    return (
        bool(leaked),
        f"{len(leaked)}/{len(_unsafe_targets())} 个危险目标被判为可抓取: {leaked}"
        if leaked
        else f"{len(_unsafe_targets())} 个危险目标全部被拒绝",
    )


# ── 2. 非 HTTP scheme 未拦截 ─────────────────────────────────────
def p_scheme_allowlist():
    """只应放行 http/https。"""
    from erc8004 import is_safe_http_url

    weird = ["file:///etc/passwd", "ftp://evil.com/x", "data:text/plain,hi", "gopher://x/"]
    kept = [u for u in weird if is_safe_http_url(u)[0]]
    return (
        bool(kept),
        f"{len(kept)}/{len(weird)} 个非 HTTP scheme 未被拦截: {kept}"
        if kept
        else "非 HTTP scheme 全部被拒绝",
    )


def p_legitimate_urls_allowed():
    """对照: 正常的外网 URL 不能被误杀, 否则防护等于瘫痪了功能。"""
    from erc8004 import is_safe_http_url

    good = [
        "https://ipfs.io/ipfs/bafybeigdyrz/1.json",
        "https://example.com/agent.json",
        "http://example.com:8080/a.json",
    ]
    blocked = [u for u in good if not is_safe_http_url(u)[0]]
    return (
        bool(blocked),
        f"正常 URL 被误杀: {blocked}" if blocked else f"{len(good)} 个正常 URL 均放行",
    )


# ── 3. 响应体无大小上限 ─────────────────────────────────────────
def p_unbounded_response():
    """一个返回数 GB 的端点就能打满索引器内存, 而 URI 由攻击者控制。"""
    import inspect

    from erc8004 import ERC8004Indexer

    src = inspect.getsource(ERC8004Indexer.fetch_registration)
    # 既要有声明长度检查, 也要有实际读取封顶 —— 光看 Content-Length 不够,
    # 那个头可以撒谎。
    declared = "content-length" in src.lower()
    streamed = "aiter_bytes" in src.lower() or "iter_bytes" in src.lower()
    capped = "MAX_REGISTRATION_BYTES" in src
    ok = declared and streamed and capped
    return (
        not ok,
        (
            f"声明长度检查={declared} 流式读取={streamed} 总量封顶={capped} "
            f"-> {'完整' if ok else '防护不全'}"
        ),
    )


# ── 4. 扫描失败区间被静默丢弃 ──────────────────────────────────
def p_silent_scan_gap():
    """RPC 限流在公共节点上很常见。索引器静默少数据, 而基于它统计出来的
    "四类覆盖数"看起来完全正常 —— 不完整的索引比没有索引更危险。"""
    import inspect

    from erc8004 import ERC8004Indexer

    src = inspect.getsource(ERC8004Indexer.scan_minted_agents)
    records_gap = "gaps.append" in src
    returns_it = "ScanResult(" in src
    ok = records_gap and returns_it
    return (
        not ok,
        f"记录失败区间={records_gap} 随结果返回={returns_it} -> "
        f"{'调用方可见' if ok else '调用方无从得知索引不完整'}",
    )


# ── 5. 索引落盘非原子 ───────────────────────────────────────────
def p_non_atomic_write():
    """该文件是 FastAPI 后端与前端的唯一数据源, 写坏整条链路挂掉。"""
    import inspect

    import erc8004

    src = inspect.getsource(erc8004._main)
    tmp_then_replace = "os.replace" in src and ".tmp" in src
    return (
        not tmp_then_replace,
        (
            "直接 open(w) 覆盖写, 无临时文件+rename -> 中途失败会留下损坏文件"
            if not tmp_then_replace
            else "已使用临时文件 + os.replace 原子写入"
        ),
    )


# ── 6. classify 的并列决胜依赖 dict 顺序 ────────────────────────
def p_classify_tiebreak():
    """max(scores, key=scores.get) 在多类别并列时取 dict 里靠前的那个,
    即结果取决于 CATEGORIES 的定义顺序 —— 隐式耦合, 改配置会静默改变分类。"""
    import erc8004

    # 构造同时命中两个类别且命中数相同的输入
    first_cat = next(iter(erc8004.CATEGORIES))
    kws = list(erc8004.CATEGORIES.values())
    if len(kws) < 2:
        return (False, "类别不足两个, 无法构造并列")
    kw_a, kw_b = kws[0][0], kws[1][0]
    cat_a, cat_b = list(erc8004.CATEGORIES)[0], list(erc8004.CATEGORIES)[1]

    reg = {"name": f"{kw_a} {kw_b}", "description": "", "tags": [], "services": [], "supportedTrust": []}
    got, conf = erc8004.ERC8004Indexer.classify(reg)

    # 反转 CATEGORIES 的定义顺序, 看结果是否改变
    original = dict(erc8004.CATEGORIES)
    erc8004.CATEGORIES.clear()
    erc8004.CATEGORIES.update(dict(reversed(list(original.items()))))
    try:
        got_rev, _ = erc8004.ERC8004Indexer.classify(reg)
    finally:
        erc8004.CATEGORIES.clear()
        erc8004.CATEGORIES.update(original)

    flipped = got != got_rev
    return (
        flipped,
        f"并列输入命中 {cat_a}/{cat_b} 各 1 次 -> 原顺序判为 {got}, "
        f"反转定义顺序后判为 {got_rev} "
        f"{'-> 结果依赖配置书写顺序' if flipped else '-> 稳定'}",
    )


# ── 7. reputation decimals 无上界 ───────────────────────────────
def p_reputation_decimals_guard():
    """score = value / (10 ** decimals)。decimals 来自链上, 若返回异常大的
    值, 10**N 会构造出巨大整数(内存与时间开销)。"""
    import inspect

    from erc8004 import ERC8004Indexer

    src = inspect.getsource(ERC8004Indexer.get_reputation)
    guarded = "MAX_REPUTATION_DECIMALS" in src
    return (
        not guarded,
        (
            "10 ** decimals 无上界: 恶意/异常合约返回大 decimals 时会构造巨大整数"
            if not guarded
            else "decimals 已有上界保护"
        ),
    )


def p_ipfs_uri_conversion():
    """对照项: ipfs:// 转换这条正常路径必须仍然工作。"""
    from erc8004 import ERC8004Indexer

    got = ERC8004Indexer.resolve_uri("ipfs://bafybeigdyrz/1.json")
    want = "https://ipfs.io/ipfs/bafybeigdyrz/1.json"
    return (got != want, f"ipfs:// -> {got} {'(正确)' if got == want else '(异常)'}")


for nm, fn in [
    ("SSRF: 链上 URI 指向内网/元数据服务", p_ssrf_internal_uri),
    ("非 HTTP scheme 未拦截", p_scheme_allowlist),
    ("响应体无大小上限", p_unbounded_response),
    ("扫描失败区间被静默丢弃", p_silent_scan_gap),
    ("索引落盘非原子", p_non_atomic_write),
    ("classify 并列决胜依赖配置顺序", p_classify_tiebreak),
    ("reputation decimals 无上界", p_reputation_decimals_guard),
    ("[对照] ipfs:// 转换仍正常", p_ipfs_uri_conversion),
    ("[对照] 正常外网 URL 未被误杀", p_legitimate_urls_allowed),
]:
    probe(nm, fn)

print()
print("=" * 78)
hits = [r for r in results if r[0]]
print(f"命中 {len(hits)} / {len(results)}")
print("=" * 78)
