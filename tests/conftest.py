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

# 可能被外部 (.env / CI secrets) 注入、且会改变测试行为的环境变量。
VOLATILE_ENV_VARS = (
    "KEEPERHUB_API_KEY",
    "KEEPERHUB_MCP_URL",
    "DRY_RUN",
    "CHAIN_ID",
    "WALLET_ADDRESS",
    "MONITOR_ADDRESS",
    "MAX_REBALANCE_USD",
    # 对账器走独立节点, 测试里必须彻底断开, 否则会真的发出网络请求
    "SEPOLIA_RPC_URL",
)

# ---------------------------------------------------------------------------
# 模块级清理: 必须发生在 src.config 被 import **之前**。
#
# src/config.py 在模块级就快照了 os.getenv("KEEPERHUB_API_KEY"), 而
# KeeperHubClient.__init__ 的签名是 `api_key: str = config.KEEPERHUB_API_KEY`
# —— 默认参数同样在 import 时求值。这意味着一旦 config 带着真 Key 被 import,
# 后续任何 monkeypatch 都救不回来: 改 os.environ 无效, 改 config 常量也无效
# (默认值早就绑定了)。唯一的拦截点就是这里。
#
# conftest.py 是 pytest 最先 import 的文件, 早于任何测试模块, 所以在这里清
# 环境变量是唯一能保证 src.config 快照到空值的地方。
# ---------------------------------------------------------------------------
for _var in VOLATILE_ENV_VARS:
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境变量: 测试不应依赖本机 .env, 也绝不能用真 API Key。

    这层是运行时防御, 负责挡住测试**内部**自己 setenv 之后泄漏到别的用例
    (例如 test_executor.py 里模拟"有 Key"的场景)。它挡不住 import 期快照,
    那一层由上面的模块级清理 + pytest.ini 的 testpaths 负责。
    """
    for var in VOLATILE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # 直接读 config 常量的调用点 (区别于 keeperhub_client 的默认参数绑定),
    # 这里一并归零, 免得将来新增代码时踩回同一个坑。
    import config as config_module

    monkeypatch.setattr(config_module, "KEEPERHUB_API_KEY", "", raising=False)


@pytest.fixture(autouse=True)
def audit_path(tmp_path, monkeypatch):
    """把 Executor 的审计输出重定向到临时目录, 保护真实 logs/audit.jsonl。

    必须是 autouse: 曾经因为只有部分测试显式请求这个 fixture, 其余测试
    直接把 dry_run 记录写进了真实审计日志（多出 PROTECT/QUOTE/TELEPORT
    三条噪音）。审计日志是对账的输入, 被污染会直接影响对账结论。
    """
    import executor as executor_module

    fake = tmp_path / "audit.jsonl"
    monkeypatch.setattr(executor_module, "_AUDIT_PATH", str(fake))
    return fake
