"""探查 KeeperHub MCP 当前暴露的全部工具与参数 schema。

用法:
    python scripts/probe_mcp_tools.py            # 列出所有工具名
    python scripts/probe_mcp_tools.py <关键字>    # 只看该关键字相关的工具

目的: 确认 execute_contract_call / execute_protocol_action 是否仍支持直连,
以及 supply 类动作需要的确切参数, 避免照着过期的假设写代码。
"""
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = os.path.join(_BASE, ".env")
if os.path.exists(_ENV):
    with open(_ENV, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, os.path.join(_BASE, "src"))

from keeperhub_client import KeeperHubClient, MCPError  # noqa: E402


def main():
    keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None
    client = KeeperHubClient()
    client._ensure_session()

    res = client._send("tools/list", {}, msg_id=client._next_id())
    tools = (res or {}).get("result", {}).get("tools", [])
    print(f"[*] MCP 共暴露 {len(tools)} 个工具\n")

    for t in tools:
        name = t.get("name", "?")
        if keyword and keyword not in name.lower():
            continue
        desc = (t.get("description") or "").split("\n")[0][:120]
        print(f"--- {name}")
        print(f"    {desc}")
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if props:
            for pname, pinfo in props.items():
                mark = "*" if pname in required else " "
                ptype = (pinfo.get("type") or "?")
                pdesc = (pinfo.get("description") or "").split("\n")[0][:70]
                print(f"      {mark} {pname}: {ptype}  {pdesc}")
        print()

    if keyword:
        print(f"[*] 仅显示匹配 '{keyword}' 的工具")


if __name__ == "__main__":
    main()
