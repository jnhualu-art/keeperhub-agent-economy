"""pytest 全局配置: 把 src/ 加入 import 路径, 并隔离环境变量。

平铺模块结构 (src/ 下直接 import), 测试统一从 conftest 注入路径。
autouse fixture 清掉可能影响行为的 kept 环境变量 (如 KEEPERHUB_API_KEY),
保证测试永远走 dry_run / 无 Key 分支, 绝不触碰真实 API。
"""

import os
import sys

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境变量: 测试不应依赖本机 .env, 也绝不能用真 API Key。"""
    for var in (
        "KEEPERHUB_API_KEY",
        "KEEPERHUB_MCP_URL",
        "DRY_RUN",
        "CHAIN_ID",
        "WALLET_ADDRESS",
        "MONITOR_ADDRESS",
        "MAX_REBALANCE_USD",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def audit_path(tmp_path, monkeypatch):
    """把 Executor 的审计输出重定向到临时目录, 保护真实 logs/audit.jsonl。"""
    import executor as executor_module

    fake = tmp_path / "audit.jsonl"
    monkeypatch.setattr(executor_module, "_AUDIT_PATH", str(fake))
    return fake
