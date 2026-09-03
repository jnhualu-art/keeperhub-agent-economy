"""统一的 .env 加载 —— 消除各脚本里重复的样板代码。

项目此前在 scripts/ 下每个脚本都各自内联了一段读取项目根 .env 的代码。
抽到这里后, 任何入口只需:

    import env
    env.load()          # 在 import config / keeperhub_client 之前调用

约定:
  - .env 位于项目根目录 (src/ 的上一级)
  - 已存在的环境变量优先, 不被 .env 覆盖 (os.environ.setdefault 语义),
    这样 CI / 部署环境注入的真实密钥不会被本地文件意外顶掉
"""

from __future__ import annotations

import os


def project_root() -> str:
    """项目根目录 (src/ 的上一级)"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_path() -> str:
    return os.path.join(project_root(), ".env")


def load(override: bool = False) -> bool:
    """把项目根 .env 注入 os.environ。

    :param override: True 时 .env 的值会覆盖已存在的环境变量 (默认 False)
    :returns: 是否成功读取到 .env
    """
    path = env_path()
    if not os.path.exists(path):
        return False

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            # 去掉可能的引号包裹
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if override:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)
    return True
