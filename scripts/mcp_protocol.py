"""MCP 协议自适应层(纯函数,可独立测试).

覆盖两代协议:
  - 会话式(2025-03-26 / 2025-06-18 / 2025-11-25):initialize 握手 +
    notifications/initialized + Mcp-Session-Id / MCP-Protocol-Version 头
  - 无状态(2026-07-28):无握手,元数据进 _meta,必带 Mcp-Method / Mcp-Name 头

本模块只放常量与纯函数(版本协商、SSE 解析);HTTP 与状态管理在 client.py。
规范出处见 docs/技能内置MCP-client完整实现-方案设计.md §二。
"""
from __future__ import annotations

import json
from typing import Any

import skill_config

# === 协议常量 ===

# JSON-RPC 标准错误码
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
# 2026-07-28:版本不支持(带 data.supported 列表)
RPC_UNSUPPORTED_PROTOCOL_VERSION = -32022

# _meta 键(2026-07-28)
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

# HTTP 头
HEADER_SESSION_ID = "Mcp-Session-Id"
HEADER_PROTOCOL_VERSION = "MCP-Protocol-Version"
HEADER_MCP_METHOD = "Mcp-Method"
HEADER_MCP_NAME = "Mcp-Name"

# 结果类型(2026-07-28;缺失视为 complete)
RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_INPUT_REQUIRED = "input_required"


# === 模式与协商 ===

MODE_SESSION = "session"
MODE_STATELESS = "stateless"


def is_stateless(version: str) -> bool:
    """判定协商出的版本是否走无状态行为。"""
    return version == skill_config.STATELESS_PROTOCOL_VERSION


def pick_common_version(server_versions: list[str]) -> str | None:
    """从 server 声明的支持列表中选最高共同版本(降序优先)。

    纯函数。server_versions 元素允许非字符串(容错),返回 None 表示无共同版本。
    """
    supported = set()
    for v in server_versions or []:
        if isinstance(v, str):
            supported.add(v)
    for ours in skill_config.SUPPORTED_PROTOCOL_VERSIONS:
        if ours in supported:
            return ours
    return None


def extract_supported_versions(err: dict[str, Any]) -> list[str]:
    """从 JSON-RPC 错误体提取 server 声明的支持版本列表。

    兼容两种形态:
      - 2026-07-28:-32022 UnsupportedProtocolVersionError(data.supported)
      - 旧版 initialize 失败:-32602 "Unsupported protocol version"(data.supported)
    其他错误返回空列表(调用方回退到"提议版本重试"策略)。
    """
    if not isinstance(err, dict):
        return []
    data = err.get("data")
    if not isinstance(data, dict):
        return []
    raw = data.get("supported")
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str)]
    return []


def method_not_recognized(err: dict[str, Any] | None, http_status: int | None) -> bool:
    """判断 server/discover 探针失败是否等价于"server 不支持新协议方法"。

    -32601 Method not found(或 HTTP 404)→ server 是旧会话式实现;
    其他错误(网络/401 等)不能下此结论。
    """
    if http_status == 404:
        return True
    if isinstance(err, dict) and err.get("code") == RPC_METHOD_NOT_FOUND:
        return True
    return False


# === SSE 解析 ===

def parse_sse_messages(text: str) -> list[dict[str, Any]]:
    """解析 SSE 文本为 JSON-RPC 消息列表(纯函数)。

    Streamable HTTP 的响应可能是 SSE 流:若干 "event: <name>" / 多行
    "data: <json>" 事件,以空行分隔。本函数:
      - 按空行切分事件;
      - 每个事件的全部 data: 行拼接(去掉行首 "data:" 与一个空格)后整体
        JSON 解析(MCP 消息可能跨多行 data);
      - 跳过解析失败的事件(不抛,调用方以"未找到目标响应"兜底);
      - 兼容旧实现的单行 data 情形(同一解析路径自然覆盖)。
    """
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                _try_append(messages, "\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            payload = line[5:]
            if payload.startswith(" "):
                payload = payload[1:]
            data_lines.append(payload)
        # event:/id:/retry: 行对 JSON-RPC 分派无影响,忽略
    if data_lines:
        _try_append(messages, "\n".join(data_lines))
    return messages


def _try_append(messages: list[dict[str, Any]], raw: str) -> None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return
    if isinstance(obj, list):
        messages.extend(m for m in obj if isinstance(m, dict))
    elif isinstance(obj, dict):
        messages.append(obj)


def pick_response(
    messages: list[dict[str, Any]], request_id: int | str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """从 SSE 消息列表中挑出目标响应,并返回需要处理的其余消息。

    返回 (response, others):
      - response: id 匹配且含 result 或 error 的 JSON-RPC 响应;
      - others: 其余消息(notification,或 id 不匹配的 server→client 请求),
        交由 client 的流内处理器(notification 记日志 / request 按能力应答)。
    纯函数。
    """
    response: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []
    for msg in messages:
        if "method" in msg:
            others.append(msg)
            continue
        if msg.get("id") == request_id and ("result" in msg or "error" in msg):
            response = msg
            continue
        others.append(msg)
    return response, others
