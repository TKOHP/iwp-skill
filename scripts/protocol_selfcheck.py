"""MCP 协议自检 CLI:诊断 skill client 与目标 endpoint 的协商与调用链路。

用法(开箱诊断):
    python -m scripts.protocol_selfcheck            # 用 skill_config 默认 endpoint
    python -m scripts.protocol_selfcheck --endpoint http://127.0.0.1:8000/mcp

输出:授权态 / 协商结果(mode+version+serverInfo) / tools/list 计数与样例 /
      一次只读工具调用抽查(list_subjects)。
退出码:0=全通过;1=存在失败项。
"""
from __future__ import annotations

import argparse
import json
import sys

import skill_config
from scripts import token_store
from scripts.client import McpAuthExpiredError, McpClient, McpToolError


def main() -> int:
    parser = argparse.ArgumentParser(description="iwp skill MCP 协议自检")
    parser.add_argument("--endpoint", default=None, help="MCP endpoint(默认取 skill_config)")
    args = parser.parse_args()

    endpoint = (args.endpoint or skill_config.MCP_TOOLS_ENDPOINT).rstrip("/")
    failures = 0

    print(f"endpoint: {endpoint}")
    print(f"client 支持版本: {skill_config.SUPPORTED_PROTOCOL_VERSIONS}")

    tokens = token_store.load()
    if not tokens:
        print("[FAIL] 无本地 token —— 请先运行 auth.ensure_authorized() 完成授权")
        return 1
    print("[OK] 本地 token 存在")

    with McpClient(mcp_endpoint=endpoint) as client:
        # 1. 协商(首次调用触发)
        try:
            client._ensure_session({"Authorization": f"Bearer {tokens['access_token']}"})
            print(f"[OK] 协商: mode={client.mode} version={client.protocol_version}")
            if client.server_info:
                info = client.server_info
                print(f"     serverInfo: {info.get('name')} v{info.get('version')}")
        except McpToolError as exc:
            print(f"[FAIL] 协商失败: [{exc.code}] {exc.message}")
            return 1

        # 2. tools/list(分页合并)
        try:
            tools = client.list_tools()
            names = [t.get("name") for t in tools]
            print(f"[OK] tools/list: {len(tools)} 个工具(分页已合并)")
            print(f"     样例: {names[:5]}")
        except McpToolError as exc:
            print(f"[FAIL] tools/list 失败: [{exc.code}] {exc.message}")
            failures += 1

        # 3. 只读工具抽查
        try:
            result = client.call_tool("list_subjects", {"page": 1, "page_size": 1})
            total = result.get("total") if isinstance(result, dict) else "?"
            print(f"[OK] list_subjects 抽查: total={total}")
        except McpAuthExpiredError as exc:
            print(f"[FAIL] 凭证失效: {exc.message}")
            failures += 1
        except McpToolError as exc:
            print(f"[FAIL] list_subjects 失败: [{exc.code}] {exc.message}")
            failures += 1

    print("RESULT:", "PASS" if failures == 0 else f"FAIL({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
