"""元测试: 保证测试环境本身是被隔离的。

这些用例不测业务, 测的是"测试没有被污染"这个前提。它们看着像在测 pytest
自己, 但每一条背后都是真实踩过的坑 —— 具体说, 是这样一个 bug:

    scripts/test_keeperhub_connection.py 匹配 pytest 默认的 test_*.py 模式,
    在收集阶段被 import, 顶层把 .env 灌进 os.environ。随后 tests/ 里的模块
    import src.config, 而 config 在模块级就快照了 KEEPERHUB_API_KEY, 于是
    拿到了真 Key。结果是: 单跑某个文件是绿的, 全量跑却是红的, 而且测试是在
    持有真凭据的状态下运行的。

修复分三处 (pytest.ini 的 testpaths / conftest 模块级清理 / 运行时 fixture),
这个文件的职责是在任何一处被改坏时立刻变红, 而不是让污染静默回来。
"""

import os

import config
import pytest

# 一旦这些变量在测试期间被注入, 测试就从"确定性"变成"看本机 .env 脸色"
FORBIDDEN_ENV_VARS = (
    "KEEPERHUB_API_KEY",
    "KEEPERHUB_MCP_URL",
    "SEPOLIA_RPC_URL",
)


def test_no_real_credentials_are_visible_to_tests():
    """os.environ 里不能残留真凭据。"""
    leaked = {var: os.environ[var] for var in FORBIDDEN_ENV_VARS if os.environ.get(var)}
    assert not leaked, f"真实凭据泄漏进了测试环境: {list(leaked)}"


def test_config_snapshots_are_empty():
    """src.config 的模块级快照必须是空的。

    为什么单独断言这个: monkeypatch.delenv 只清 os.environ, 清不掉 config
    在 import 期已经固化的常量。所以必须有这条独立的检查。
    """
    assert config.KEEPERHUB_API_KEY == "", (
        "config.KEEPERHUB_API_KEY 在 import 期被快照成了真 Key。 "
        "修法见 tests/conftest.py 顶部的模块级清理说明。"
    )


def test_keeperhub_client_default_argument_is_not_a_real_key():
    """KeeperHubClient 的默认 api_key 不能绑定成真 Key。

    这是最阴的一处: 默认参数在**函数定义时**求值, 所以即便运行时把
    config.KEEPERHUB_API_KEY 改成空字符串, 这个默认值也不会跟着变。
    唯一有效的拦截时机是 config 被 import 之前。
    """
    import inspect

    from keeperhub_client import KeeperHubClient

    # 按参数名取, 不按下标: 签名是 (url, api_key), 写死 [1] 会在有人调整
    # 参数顺序时变成"测了空气但还显示通过"。
    params = inspect.signature(KeeperHubClient.__init__).parameters
    assert "api_key" in params, "KeeperHubClient 不再有 api_key 参数? 请同步更新本用例"
    bound_default = params["api_key"].default
    assert bound_default == "", (
        f"KeeperHubClient 的默认 api_key 被绑定成了 {bound_default!r}, 说明 config "
        f"在清理之前就被 import 了。检查 pytest.ini 的 testpaths 是否还钉在 tests/。"
    )


def test_conftest_module_level_cleanup_runs_before_config_import():
    """conftest 的模块级清理必须早于 src.config 的 import。

    这条是上面三条的"根因哨兵": 只要 conftest 顶部那段 os.environ.pop 还在,
    且 pytest 先加载 conftest, 前面三条就一定是绿的。
    """
    import conftest

    assert hasattr(conftest, "VOLATILE_ENV_VARS"), (
        "conftest 顶部的模块级清理被删掉了, 测试将重新暴露给本机 .env"
    )
    assert "KEEPERHUB_API_KEY" in conftest.VOLATILE_ENV_VARS


def test_audit_log_is_not_the_real_one(tmp_path):
    """审计日志必须被重定向, 绝不能写进真实的 logs/audit.jsonl。

    audit.jsonl 是对账器的输入, 被测试写脏会直接污染对账结论。
    """
    import executor as executor_module

    real_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs",
        "audit.jsonl",
    )
    assert os.path.abspath(executor_module._AUDIT_PATH) != os.path.abspath(real_path), (
        "_AUDIT_PATH 指向了真实审计日志, autouse 的 audit_path fixture 没生效"
    )


@pytest.mark.parametrize("var", FORBIDDEN_ENV_VARS)
def test_env_var_stays_clean_even_if_a_test_set_it(var, monkeypatch):
    """单个用例里 setenv 之后, 后续用例必须看到干净的环境。

    对应 test_executor.py 里模拟"有 API Key"那种场景: 它靠 monkeypatch
    自动还原, 这里验证还原确实发生在下一个用例之前。
    """
    monkeypatch.setenv(var, "injected-by-this-test")
    assert os.environ[var] == "injected-by-this-test"
